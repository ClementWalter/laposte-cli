#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.1",
#     "curl_cffi>=0.7",
#     "rich>=13.0",
#     "browser_cookie3>=0.20",
# ]
# ///
"""La Poste CLI — send registered (LREL) or priority (LEL) mail from the terminal.

Drives the laposte.fr "Courrier En Ligne" (CEL) flow up to the payment page,
then hands off to your browser to confirm payment with a saved card. PDF only.

Auth is cookie-based: login imports an authorized browser session into the
1Password broker, with a mode-600 working copy for offline recovery.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlencode

import browser_cookie3
import click
from curl_cffi import CurlMime, requests
from rich.console import Console
from rich.table import Table

logger = logging.getLogger(__name__)
console = Console()

# --- Constants ---------------------------------------------------------------

CONFIG_DIR = Path.home() / ".config" / "laposte-cli"
CONFIG_FILE = CONFIG_DIR / "config.json"

# CEL = Courrier En Ligne. lpelPart = La Poste E-Lettre, Particulier mode.
BASE = "https://www.laposte.fr"
CEL_OCC = f"{BASE}/cel/occ/ecommerce/occ/v2/lpelPart/e-service/cel"
SENDING_URL = f"{BASE}/cel/sending/lpelPart"  # /{userId}
SERCADIA_URL = f"{BASE}/cel/address/sercadia/check"
ADDRESSES_URL = f"{BASE}/cel/address/lpelPart/account/profiles/CURRENT/postal-addresses"
PING_URL = f"{BASE}/cel/api/ping"
UPLOAD_URL = f"{CEL_OCC}/upload"
PRICE_URL = f"{CEL_OCC}/price"
RECIPIENTS_URL = f"{CEL_OCC}/recipients"
OPTIONS_URL = f"{CEL_OCC}/options"
CART_CREATE_URL = f"{CEL_OCC}/users/current/carts/current"
HANDOFF_URL = f"{BASE}/checkout/recapitulatif"

# Public IGN/BAN geocoder — no auth needed.
BAN_URL = "https://data.geopf.fr/geocodage/search"

# La Poste's CDN sits behind a WAF that does TLS fingerprinting; impersonating
# a recent Chrome avoids 403s that never reach the application layer.
TLS_IMPERSONATE = "chrome131"
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

POSTAGE_TYPES = {"recommande": "LREL", "lrel": "LREL", "lettre-rouge": "LEL", "lel": "LEL"}


# --- Config + browser cookie extraction --------------------------------------

# Which browsers browser_cookie3 supports. We default to chrome which is what
# 99% of users have; the user can override with `--browser firefox/safari/edge`
# if their laposte.fr session lives somewhere else.
SUPPORTED_BROWSERS = {
    "chrome": "chrome",
    "firefox": "firefox",
    "safari": "safari",
    "edge": "edge",
    "brave": "brave",
    "chromium": "chromium",
    "opera": "opera",
    "arc": "arc",
}



def _auth_broker(action, payload=None):
    """Keep credential bodies on pipes and suppress provider errors containing secrets."""
    import subprocess
    import json
    try:
        result = subprocess.run(
            ["claudine-secret", "auth", action, "laposte"],
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode not in (0, 3):
            return None
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return None
        if action == "load" and result.returncode:
            return None
        return value
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def load_config() -> dict:
    """Prefer broker pending or vault credentials over a legacy working copy."""
    if CONFIG_FILE.with_suffix(".auth-pending").exists():
        return _legacy_config()
    return _auth_broker("load") or _legacy_config()


def _legacy_config() -> dict:
    """Return stored config (preferred browser etc.), or empty dict."""
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save_config(config: dict) -> None:
    """Persist config; chmod 600 since it may hold preferences."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.touch(mode=0o600, exist_ok=True)
    CONFIG_FILE.chmod(0o600)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    CONFIG_FILE.chmod(0o600)
    pending = CONFIG_FILE.with_suffix(".auth-pending")
    if _auth_broker("save", config) is None:
        pending.touch(mode=0o600)
    else:
        pending.unlink(missing_ok=True)


def get_browser_cookies(browser: str = "chrome") -> tuple[str, str]:
    """Extract laposte.fr cookies from a local browser and return (header, userId).

    Reads the cookie store of the named browser directly. On macOS Chrome this
    pops a Keychain dialog the first time. We pull cookies for both
    `laposte.fr` and `moncompte.laposte.fr` so the SSO bounce can complete
    server-side without ever opening a browser.
    """
    fn = getattr(browser_cookie3, SUPPORTED_BROWSERS.get(browser, browser), None)
    if fn is None:
        raise click.ClickException(
            f"Unknown browser '{browser}'. Try: {', '.join(SUPPORTED_BROWSERS)}"
        )

    jar = fn(domain_name="laposte.fr")
    cookies = list(jar)
    if not cookies:
        raise click.ClickException(
            f"No laposte.fr cookies in {browser}. Log in to laposte.fr in {browser} "
            "first, then re-run."
        )

    # Build a Cookie header from the jar. Duplicates may exist across paths;
    # the last one wins, which matches what the browser sends.
    cookie_dict: dict[str, str] = {}
    user_id: str | None = None
    for c in cookies:
        cookie_dict[c.name] = c.value
        if c.name == "pa_user":
            try:
                user_id = json.loads(unquote(c.value)).get("id")
            except Exception:
                pass
    if not user_id:
        raise click.ClickException(
            "Could not read pa_user cookie. Are you logged in to laposte.fr "
            f"in {browser}?"
        )
    header = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    return header, user_id


def get_browser_jar(browser: str = "chrome"):
    """Same as get_browser_cookies but returns the full CookieJar (for sessions)."""
    fn = getattr(browser_cookie3, SUPPORTED_BROWSERS.get(browser, browser), None)
    if fn is None:
        raise click.ClickException(f"Unknown browser '{browser}'.")
    return fn(domain_name="laposte.fr")


def require_login() -> dict:
    """Return a session bundle with cookies + userId pulled from the browser.

    `cookies` is a CookieJar (not a header string) so curl_cffi can rotate
    it from `Set-Cookie` responses. `sendingId` is the value of the
    `lpel_cel` cookie — the active server-side draft handle.
    """
    cfg = load_config()
    browser = cfg.get("browser", "chrome")
    if cfg.get("cookie_header") and cfg.get("userId"):
        return {"cookies": cfg["cookie_header"], "userId": cfg["userId"], "sendingId": None, "browser": browser}
    try:
        jar = get_browser_jar(browser)
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(
            f"Could not read cookies from {browser}: {exc}. "
            "Run 'laposte-cli login --browser <name>' to pick a different one."
        )

    user_id: str | None = None
    sending_id: str | None = None
    for c in jar:
        if c.name == "pa_user":
            try:
                user_id = json.loads(unquote(c.value)).get("id")
            except Exception:
                pass
        elif c.name == "lpel_cel":
            sending_id = c.value
    if not user_id:
        raise click.ClickException(
            f"Not logged in to laposte.fr in {browser}. Open laposte.fr "
            "there, log in, then retry."
        )
    return {"cookies": jar, "userId": user_id, "sendingId": sending_id, "browser": browser}


# --- Cookie helpers ----------------------------------------------------------


def parse_cookie_header(s: str) -> dict[str, str]:
    """Split a `name=value; name=value` cookie string into a dict."""
    out: dict[str, str] = {}
    for kv in s.split(";"):
        kv = kv.strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def extract_user_id(cookies: dict[str, str]) -> str | None:
    """Pull the numeric userId out of the URL-encoded `pa_user` JSON cookie."""
    pa = cookies.get("pa_user")
    if not pa:
        return None
    try:
        return json.loads(unquote(pa)).get("id")
    except Exception:
        return None


# --- HTTP session ------------------------------------------------------------


def make_session(cookie_source: str | object) -> requests.Session:
    """Build a curl_cffi session impersonating Chrome with our cookies.

    Accepts either a raw `Cookie:` header string (for the legacy --cookie-header
    path) or a CookieJar-like iterable of `Cookie` objects (the live extraction
    path). Setting cookies via `s.cookies.set(...)` rather than the header lets
    curl_cffi auto-rotate them from `Set-Cookie` responses — required because
    La Poste's F5 BIG-IP TS* tokens have short TTLs and rotate per request.
    """
    s = requests.Session(impersonate=TLS_IMPERSONATE)
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/envoi-courrier-en-ligne/parcours/creer-lettre",
        }
    )

    if isinstance(cookie_source, str):
        # Legacy: parse "name=value; name=value" into the jar.
        for kv in cookie_source.split(";"):
            kv = kv.strip()
            if "=" in kv:
                k, v = kv.split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain=".laposte.fr")
    else:
        # Cookie jar from browser_cookie3: filter to laposte.fr (drop the
        # moncompte.laposte.fr-scoped duplicates that would otherwise stomp
        # the www.laposte.fr ones with last-wins semantics).
        seen: set[str] = set()
        for c in cookie_source:
            if c.domain not in ("www.laposte.fr", ".laposte.fr", "laposte.fr"):
                continue
            if c.name in seen:
                continue
            s.cookies.set(c.name, c.value, domain=c.domain, path=c.path)
            seen.add(c.name)
    return s


# --- BAN + SERCADIA address resolution ---------------------------------------


def ban_lookup(q: str, *, postcode: str | None = None) -> list[dict]:
    """Search the public IGN/BAN geocoder for an address (housenumber level)."""
    params = {"q": q, "type": "housenumber", "limit": "5"}
    if postcode:
        params["postcode"] = postcode
    r = requests.get(BAN_URL, params=params, timeout=10, impersonate=TLS_IMPERSONATE)
    r.raise_for_status()
    return r.json().get("features", [])


def sercadia_certify(
    session: requests.Session,
    street: str,
    zip_code: str,
    city: str,
    *,
    additional: str = "",
) -> dict | None:
    """Ask La Poste's SERCADIA to RNVP-certify an address and return the ceaid block.

    Endpoint returns a JSON array of candidate matches (usually 1); we return
    the first match or None if nothing came back.
    """
    params = {"streetName": street, "place": f"{zip_code} {city}"}
    if additional:
        params["additionalStreetName"] = additional
    r = session.get(SERCADIA_URL, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, list):
        return data[0] if data else None
    if isinstance(data, dict):
        return data
    return None


# --- Draft helpers -----------------------------------------------------------


def today_dmy() -> str:
    """Today's date in La Poste's DD/MM/YYYY format."""
    return _dt.date.today().strftime("%d/%m/%Y")


def fetch_sender_addresses(session: requests.Session) -> list[dict]:
    """Return the user's saved sender (postal) addresses.

    The endpoint returns a flat dict keyed by postalId; we adapt each entry
    into the shape the /sending draft expects (id, streetName, zipCode,
    city, isPrimary, label, firstName, lastName…) so callers don't have to
    know about the two representations.
    """
    r = session.get(ADDRESSES_URL, timeout=15)
    r.raise_for_status()
    data = r.json()

    raw_items: list[dict] = []
    if isinstance(data, dict) and any(k.startswith("P-") for k in data):
        raw_items = list(data.values())
    elif isinstance(data, dict):
        raw_items = data.get("postalAddresses") or data.get("addresses") or []
    elif isinstance(data, list):
        raw_items = data

    addresses: list[dict] = []
    for raw in raw_items:
        line1 = (raw.get("address") or {}).get("line1", "").strip()
        line4 = (raw.get("address") or {}).get("line4", "").strip()
        # line1 looks like "M. PRENOM NOM" — split civility from names.
        sex = "MALE"
        names = line1.split()
        if names and names[0].upper() in {"M.", "M", "MR", "MONSIEUR"}:
            names = names[1:]
        elif names and names[0].upper() in {"MME.", "MME", "MADAME", "MS", "MRS"}:
            sex = "FEMALE"
            names = names[1:]
        first = names[0] if names else ""
        last = " ".join(names[1:]) if len(names) > 1 else ""
        addresses.append(
            {
                "id": raw.get("postalId"),
                "label": raw.get("label", ""),
                "streetName": line4,
                "zipCode": raw.get("postalCode", ""),
                "city": raw.get("locality", ""),
                "country": raw.get("countryCode", "FR"),
                "ceaid": raw.get("ceaId"),
                "isCompany": raw.get("isBtoB", False),
                "isPrimary": raw.get("isPrimary", False),
                "firstName": first,
                "lastName": last,
                "sex": sex,
                "rnvpChecked": raw.get("rnvpChecked", False),
                "rnvpCheckMethod": raw.get("rnvpCheckMethod", ""),
                "rnvpValidation": raw.get("rnvpValidation", ""),
                "additionalFloor": "",
            }
        )
    return addresses


def pick_sender(addresses: list[dict], hint: str | None) -> dict:
    """Pick the user's sender address: by id, label, or default to primary."""
    if hint:
        for a in addresses:
            if a.get("id") == hint or a.get("label", "").lower() == hint.lower():
                return a
        raise click.ClickException(
            f"Sender address '{hint}' not found. Use 'laposte-cli addresses'."
        )
    primary = next((a for a in addresses if a.get("isPrimary")), None)
    if primary:
        return primary
    if addresses:
        return addresses[0]
    raise click.ClickException("No saved sender address. Add one on laposte.fr first.")


def upload_pdf(
    session: requests.Session,
    pdf_path: Path,
    *,
    sending_id: str | None,
    postage_type: str,
    priority: int,
) -> dict:
    """Upload one PDF and return the document descriptor returned by the server.

    Pass `sending_id=None` on the very first upload of a session — the server
    allocates a fresh draft and returns its UUID in the response (`sendingId`
    field). Subsequent uploads must pass that same UUID; the SPA omits the
    `sendingId` form field when it's null and includes it otherwise, which
    is what we mirror here.

    Returns the server's celDocumentResponseWsDTO: `{id, name, pagesCount,
    sheetsCount, priority, sendingId, size, source, type, ...}`.
    """
    pages_count = _pdf_page_count(pdf_path)
    size = pdf_path.stat().st_size

    mp = CurlMime()
    mp.addpart(name="name", data=pdf_path.name)
    mp.addpart(
        name="fileDocument",
        filename=pdf_path.name,
        content_type="application/pdf",
        data=pdf_path.read_bytes(),
    )
    mp.addpart(name="documentPriority", data=str(priority))
    mp.addpart(name="pagesCount", data=str(pages_count))
    mp.addpart(name="postageType", data=postage_type)
    mp.addpart(name="source", data="upload")
    mp.addpart(name="totalSize", data=str(size))
    if sending_id is not None:
        mp.addpart(name="sendingId", data=sending_id)
    mp.addpart(name="frontPostageType", data=postage_type)

    r = session.post(UPLOAD_URL, multipart=mp, timeout=120)
    if not r.ok:
        raise click.ClickException(
            f"Upload failed for {pdf_path.name}: {r.status_code} {r.text[:300]}"
        )
    return r.json()


def _pdf_page_count(path: Path) -> int:
    """Count pages by scanning for /Type /Page tokens in the raw PDF stream.

    Quick and dependency-free; works for the well-formed PDFs La Poste accepts.
    """
    data = path.read_bytes()
    # Count `/Type /Page` not followed by 's' (excludes /Pages).
    import re

    count = len(re.findall(rb"/Type\s*/Page\b(?!s)", data))
    return max(count, 1)


def parse_recipient(spec: str, session: requests.Session) -> dict:
    """Turn a `--to` CLI spec into a full RNVP-certified receiver object.

    Format: "Civilité Prenom Nom | street | zip city" with `|` as separator.
    Civilité is M or MME (defaults to M). Example:
        "M Jean Dupont|1 rue de la Paix|75001 PARIS"
    """
    parts = [p.strip() for p in spec.split("|")]
    if len(parts) < 3:
        raise click.ClickException(
            f"Recipient '{spec}' invalid. Expected 'Civ Prenom Nom|street|cp ville'."
        )
    person, street, place = parts[0], parts[1], parts[2]
    additional = parts[3] if len(parts) > 3 else ""

    tokens = person.split()
    if tokens and tokens[0].upper() in {"M", "M.", "MR", "MONSIEUR"}:
        sex = "MALE"
        names = tokens[1:]
    elif tokens and tokens[0].upper() in {"MME", "MME.", "MADAME", "MS", "MRS"}:
        sex = "FEMALE"
        names = tokens[1:]
    else:
        sex = "MALE"
        names = tokens
    first_name = names[0] if names else ""
    last_name = " ".join(names[1:]) if len(names) > 1 else ""

    place_tokens = place.split(None, 1)
    if len(place_tokens) < 2:
        raise click.ClickException(f"Recipient '{spec}': 'cp ville' part missing.")
    zip_code, city = place_tokens[0], place_tokens[1]

    cert = sercadia_certify(session, street, zip_code, city, additional=additional)
    # SERCADIA returns the receiver shape directly; fall back to raw fields if not.
    if not cert or not isinstance(cert, dict):
        raise click.ClickException(f"SERCADIA returned no result for '{street} {zip_code} {city}'.")

    # Merge SERCADIA payload with our person/civility data.
    receiver = {
        "country": "FR",
        "isCompany": False,
        "firstName": first_name,
        "lastName": last_name,
        "streetName": cert.get("streetName") or street,
        "additionalStreetName": additional,
        "additionalBuilding": "",
        "additionalFloor": "",
        "zipCode": cert.get("zipCode") or zip_code,
        "city": cert.get("city") or city,
        "fullName": f"{'M' if sex == 'MALE' else 'MME'} {first_name} {last_name}".upper(),
        "sex": sex,
        "rnvpChecked": True,
        "rnvpCheckMethod": cert.get("rnvpCheckMethod") or "code verified",
        "rnvpValidation": cert.get("rnvpValidation") or "verified",
        "ceaid": cert.get("ceaid"),
        "numCountryCode": cert.get("numCountryCode") or "250",
        "ceaidLine6": cert.get("ceaidLine6"),
        "mascadiaType": "user",
        "highlight": False,
    }
    if not receiver["ceaid"]:
        raise click.ClickException(
            f"SERCADIA could not RNVP-certify '{street} {zip_code} {city}'. "
            "Try a different street/postal code."
        )
    return receiver


# --- The core draft → cart flow ---------------------------------------------


def build_draft(
    *,
    user_id: str,
    sending_id: str,
    documents: list[dict],
    receivers: list[dict],
    sender: dict | None,
    postage_type: str,
    notice_of_receipt: bool,
    postal_tracking: bool,
    duplex: bool,
    color: bool,
    deposit_date: str,
) -> dict:
    """Assemble the full draft state the SPA syncs to /cel/sending/lpelPart/{userId}."""
    is_lrel = postage_type == "LREL"
    fmt = {
        "name": "Lettre recommandée" if is_lrel else "e-lettre rouge",
        "enabled": True,
        "type": postage_type,
    }
    other = {
        "name": "e-lettre rouge" if is_lrel else "Lettre recommandée",
        "enabled": True,
        "type": "LEL" if is_lrel else "LREL",
    }
    return {
        "letterNameEdited": None,
        "userId": user_id,
        "currentMode": "PART",
        "options": {
            "format": fmt,
            "interactionsFormat": fmt,
            "formatsList": [fmt, other] if is_lrel else [other, fmt],
            "prices": {},  # server fills in
            "totalPrice": {"currencyIso": "EUR", "value": 0, "formattedValue": "", "priceType": "BUY"},
            "availableDates": [],
            "enableDuplexPrintingToggle": True,
            "noticeOfReceipt": notice_of_receipt,
            "postalTracking": postal_tracking,
            "duplexPrinting": duplex,
            "colorPrinting": color,
            "isDepositDate": True,
            "depositDate": deposit_date,
            "scheduledDate": "",
            "enableImageUpload": True,
        },
        "address": {
            "redirect": None,
            "recipientId": None,
            "isSenderAddressRequired": sender is not None,
            "senderCountry": "FR",
            "receiverIndexListPostalNotMatching": [],
            "receiverIndexListAddressTooLong": [],
            "senderIndexListPostalNotMatching": [],
            "receiverCountries": ["FR"],
            "hasSenderHighlight": False,
            "hasSenderAddressErrors": False,
            "receiver": {"addressList": receivers},
            "sender": {
                "selectedAddress": sender,
                "addressList": [sender] if sender else [],
            },
        },
        "content": {
            "files": documents,
            "sendingId": sending_id,
            "limitSize": 20_000_000,
            "isUploadingFile": False,
            "isRemoving": False,
            "letterTemplatesCount": 243,
            "selectedModel": None,
            "limitRecipientCount": 200,
        },
        "letters": {"letters": []},
        "createdAt": int(_dt.datetime.now().timestamp()),
    }


def sync_draft(session: requests.Session, user_id: str, draft: dict) -> dict:
    """POST the full draft state and return the server's echoed copy."""
    r = session.post(
        f"{SENDING_URL}/{user_id}",
        json=draft,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        timeout=30,
    )
    if not r.ok:
        raise click.ClickException(f"Draft sync failed: {r.status_code} {r.text[:500]}")
    return r.json()


def calc_price(
    session: requests.Session,
    *,
    documents: list[dict],
    receivers: list[dict],
    postage_type: str,
    notice_of_receipt: bool,
    postal_tracking: bool,
    duplex: bool,
    color: bool,
    deposit_date: str,
) -> dict:
    """Ask the server to compute pricing for the current draft.

    Sends a `celConfigurationWsDTO`. This also populates server-side state
    (sheetsCount, totals) that cart creation reads later — skip it and
    /carts/current 500s with a NullPointerException on getSheetsCount().
    """
    from math import ceil

    total_pages = sum((d.get("pagesCount") or 0) for d in documents)
    total_files = len(documents)
    duplex_sheets = sum(ceil((d.get("pagesCount") or 0) / 2) for d in documents)
    sheets_count = duplex_sheets if duplex else total_pages
    # The SPA toggles addressSheet automatically based on size and presence of
    # a letter document; for an upload-only flow we mirror its logic.
    address_sheet = total_files > 0 and sheets_count >= 4

    config = {
        "type": "celConfigurationWsDTO",
        "postageType": postage_type,
        "noticeOfReceipt": notice_of_receipt,
        "postalTracking": postal_tracking,
        "duplexPrinting": duplex,
        "colorPrinting": color,
        "depositDate": deposit_date,
        "scheduledDate": "",
        "destinationAddresses": [_to_api_address(r) for r in receivers if not r.get("highlight")],
        "pagesCount": total_pages,
        "sheetsCount": sheets_count,
        "documentsCount": total_files,
        "addressSheet": address_sheet,
        "departureCountry": "FR",
        "hasInternationalAddresses": False,
        "limitRecipientCount": 200,
        "canCanceled": False,
        "deleteContent": False,
        "uploadImageDisabled": False,
    }
    r = session.post(PRICE_URL, json=config, timeout=30)
    if not r.ok:
        raise click.ClickException(f"Price calc failed: {r.status_code} {r.text[:300]}")
    return r.json()


def _to_api_address(r: dict, *, addr_type: str | None = None) -> dict:
    """Translate our draft-shaped address to the API shape /recipients wants.

    Mirrors the SPA's `normalizeLibAddressToBody` exactly:
      - `line1` = street name (NOT the recipient's name — La Poste's API
        uses postal line numbering)
      - `town` not `city`, `postalCode` not `zipCode`
      - `pobox` ← additionalStreetName, `building` ← additionalBuilding,
        `appartment` (sic) ← additionalFloor
      - `country: {isocode}` is a nested object
      - `ceaId` (capital I), not `ceaid`
      - `titleCode: "mr"/"mrs"` (the `title: "M."/"Mme"` field is layered
        on top by sendReceiverAddress; we add it too for consistency)
    """
    sex = r.get("sex")
    title_code = "mr" if sex == "MALE" else "mrs" if sex == "FEMALE" else ""
    title = "M." if sex == "MALE" else "Mme" if sex == "FEMALE" else ""
    out: dict = {
        "label": r.get("label"),
        "country": {"isocode": r.get("country") or "FR"},
        "line1": r.get("streetName") or "",
        "pobox": r.get("additionalStreetName") or "",
        "building": r.get("additionalBuilding") or "",
        "appartment": r.get("additionalFloor") or "",
        "remarks": r.get("additionalKeypad") or "",
        "postalCode": r.get("zipCode") or "",
        "postalId": r.get("id"),
        "town": r.get("city") or "",
        "firstName": r.get("firstName") or "",
        "lastName": r.get("lastName") or "",
        "titleCode": title_code,
        "title": title,
    }
    if r.get("ceaid"):
        out["ceaId"] = r["ceaid"]
    if addr_type:
        out["type"] = addr_type
    if r.get("isCompany"):
        out["companyName"] = r.get("companyName") or ""
        out["receiver"] = r.get("service") or ""
    return out


def push_recipients(
    session: requests.Session,
    receivers: list[dict],
    *,
    sending_id: str,
    postage_type: str,
) -> str | None:
    """Push recipients to the server-side draft.

    Required body: `{addresses, sendingId, postageType}`. Returns the
    server-issued `recipientId` so the cart bundle can reference this group.
    """
    body = {
        "addresses": [_to_api_address(r) for r in receivers],
        "sendingId": sending_id,
        "postageType": postage_type,
    }
    r = session.post(RECIPIENTS_URL, json=body, timeout=30)
    if not r.ok:
        console.print(
            f"[yellow]Warning:[/yellow] /recipients returned {r.status_code}: {r.text[:200]}"
        )
        return None
    # Response shape: {type, recipients: [{id, customId, ...}], sendingId}.
    # The recipientId we need is `recipients[0].id`.
    try:
        recs = (r.json() or {}).get("recipients") or []
        return recs[0].get("id") if recs else None
    except Exception:
        return None


def commit_to_cart(
    session: requests.Session,
    *,
    sending_id: str,
    recipient_id: str | None,
    documents: list[dict],
    receivers: list[dict],
    sender: dict | None,
    postage_type: str,
    notice_of_receipt: bool,
    postal_tracking: bool,
    duplex: bool,
    color: bool,
    deposit_date: str,
    letter_name: str = "Courrier",
) -> dict | None:
    """Materialize the current draft into a cart bundle.

    Posts an `addEServiceToCart` item shaped like the SPA's `occCel` builder:
    documentsConfig, destinationAddresses, postageType, etc. plus the
    sender's departureAddress when present. Server takes a few seconds and
    returns the cart payload with a `celBundleId`.
    """
    from math import ceil

    total_pages = sum((d.get("pagesCount") or 0) for d in documents)
    total_files = len(documents)
    sheets_count = (
        sum(ceil((d.get("pagesCount") or 0) / 2) for d in documents)
        if duplex else total_pages
    )
    address_sheet = total_files > 0 and sheets_count >= 4

    item = {
        "fields": "LIGHT",
        "letterName": letter_name,
        "recipientId": recipient_id or "",
        "sendingId": sending_id,
        "documentsConfig": documents,
        "postageType": postage_type,
        "duplexPrinting": duplex,
        "colorPrinting": color,
        "depositDate": deposit_date,
        "scheduledDate": "",
        "destinationAddresses": [_to_api_address(r) for r in receivers if not r.get("highlight")],
        "pagesCount": total_pages,
        "sheetsCount": sheets_count,
        "documentsCount": total_files,
        "addressSheet": address_sheet,
    }
    if sender:
        item["departureAddress"] = _to_api_address(sender)
    if postage_type == "LREL":
        item["noticeOfReceipt"] = notice_of_receipt
    else:
        item["postalTracking"] = postal_tracking

    r = session.post(CART_CREATE_URL, json=item, timeout=120)
    if not r.ok:
        raise click.ClickException(f"Cart creation failed: {r.status_code} {r.text[:500]}")
    try:
        return r.json()
    except Exception:
        return None


# --- CLI commands ------------------------------------------------------------


@click.group()
@click.option("--debug", is_flag=True, help="Verbose logging of HTTP traffic.")
def cli(debug: bool) -> None:
    """La Poste CLI — send a registered letter from your terminal."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


@cli.command()
@click.option(
    "--browser",
    default="chrome",
    type=click.Choice(list(SUPPORTED_BROWSERS), case_sensitive=False),
    help="Browser to read cookies from. Default: chrome.",
)
def login(browser: str) -> None:
    """Set the browser used to read laposte.fr cookies.

    No copy-paste required: we read your existing browser session directly.
    On macOS Chrome, the first read may pop a Keychain prompt to allow access
    to Chrome's encrypted cookie store.
    """
    header, user_id = get_browser_cookies(browser)
    s = make_session(header)
    try:
        r = s.get(PING_URL, timeout=10)
        if not r.ok:
            console.print(f"[yellow]Warning:[/yellow] ping returned {r.status_code}")
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] could not ping La Poste: {exc}")

    save_config({"browser": browser, "cookie_header": header, "userId": user_id})
    console.print(
        f"[green]✓[/green] Logged in as user [cyan]{user_id}[/cyan] via {browser}"
    )
    console.print(
        "  Session saved through the 1Password broker with a protected local working copy."
    )


@cli.command()
def whoami() -> None:
    """Show the active user id (read live from the browser)."""
    cfg = require_login()
    console.print(f"User ID:   [cyan]{cfg['userId']}[/cyan]")
    console.print(f"Browser:   {cfg['browser']}")
    sending = cfg.get("sendingId") or "<none yet>"
    console.print(f"lpel_cel:  {sending}")


@cli.command()
def logout() -> None:
    """Remove the browser preference (does NOT log you out of laposte.fr)."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        console.print(
            "[green]✓[/green] Cleared local preference. "
            "To fully log out, do so in your browser."
        )
    else:
        console.print("Nothing to clear.")


@cli.command()
def addresses() -> None:
    """List your saved sender addresses."""
    cfg = require_login()
    s = make_session(cfg["cookies"])
    addrs = fetch_sender_addresses(s)
    if not addrs:
        console.print("[yellow]No saved addresses.[/yellow]")
        return
    table = Table("ID", "Label", "Street", "ZIP", "City", "Primary")
    for a in addrs:
        table.add_row(
            a.get("id", ""),
            a.get("label", ""),
            a.get("streetName", ""),
            a.get("zipCode") or a.get("postalCode") or "",
            a.get("city") or a.get("locality") or "",
            "★" if a.get("isPrimary") else "",
        )
    console.print(table)


@cli.command()
@click.argument("pdfs", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--to",
    "recipients",
    multiple=True,
    required=True,
    help='Recipient: "M Prenom Nom|street|cp ville". Repeatable.',
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(list(POSTAGE_TYPES), case_sensitive=False),
    default="recommande",
    help="Postage type. Default: recommande (LREL).",
)
@click.option("--ar/--no-ar", default=False, help="Avis de réception (+1,25 €).")
@click.option("--color/--bw", default=False, help="Color printing (+0,50 €).")
@click.option("--duplex/--simplex", default=True, help="Recto-verso (default) or recto only.")
@click.option(
    "--tracking/--no-tracking",
    default=False,
    help="Postal tracking (LEL only, +0,50 €).",
)
@click.option(
    "--from",
    "sender_hint",
    default=None,
    help="Sender address id or label. Default: your primary address.",
)
@click.option(
    "--no-sender",
    is_flag=True,
    help="Do not include a return address.",
)
@click.option(
    "--date",
    "deposit_date",
    default=None,
    help="Deposit date DD/MM/YYYY. Default: today.",
)
@click.option(
    "--open/--no-open",
    "open_browser",
    default=True,
    help="Open the payment page in your browser at the end.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Upload + price check only — don't create the cart.",
)
def send(
    pdfs: tuple[Path, ...],
    recipients: tuple[str, ...],
    fmt: str,
    ar: bool,
    color: bool,
    duplex: bool,
    tracking: bool,
    sender_hint: str | None,
    no_sender: bool,
    deposit_date: str | None,
    open_browser: bool,
    dry_run: bool,
) -> None:
    """Upload PDFs and prepare a letter, then open the payment page.

    Example:
        laposte-cli send invoice.pdf \\
            --to "M Jean Dupont|1 rue de la Paix|75001 PARIS" \\
            --format recommande --ar
    """
    if not pdfs:
        raise click.ClickException("Provide at least one PDF.")
    cfg = require_login()
    user_id = cfg["userId"]
    session = make_session(cfg["cookies"])

    # Ping first to nudge the server to refresh the auth token before the
    # F5/CEL-side TS* cookies expire under our feet (we've observed flaky 401s
    # on /postal-addresses immediately after a fresh session when ping is
    # skipped).
    session.get(PING_URL, timeout=10)

    postage = POSTAGE_TYPES[fmt.lower()]
    deposit = deposit_date or today_dmy()

    # 1. Sender address.
    sender = None
    if not no_sender:
        sender_addrs = fetch_sender_addresses(session)
        sender = pick_sender(sender_addrs, sender_hint)
        console.print(f"[dim]From:[/dim] {sender.get('label')} — {sender.get('streetName')} {sender.get('zipCode') or sender.get('postalCode')} {sender.get('city') or sender.get('locality')}")

    # 2. Resolve + RNVP-certify recipients.
    console.print(f"[dim]Resolving {len(recipients)} recipient(s)…[/dim]")
    receivers = [parse_recipient(r, session) for r in recipients]
    for r in receivers:
        console.print(f"  → {r['fullName']} — {r['streetName']} {r['zipCode']} {r['city']} [dim](ceaid={r['ceaid']})[/dim]")

    # 3. Wipe any leftover server-side draft (idempotent — returns "No state
    # to delete" if there's none), then upload PDFs. The first /upload omits
    # the sendingId field; the server allocates a fresh draft and returns its
    # UUID, which we thread into subsequent uploads (each new file bumps the
    # `documentPriority` counter — the SPA starts at 2 and increments).
    session.delete(f"{SENDING_URL}/{user_id}", timeout=15)

    sending_id: str | None = None
    documents: list[dict] = []
    console.print(f"[dim]Uploading {len(pdfs)} PDF(s)…[/dim]")
    for i, pdf in enumerate(pdfs):
        priority = (documents[-1]["priority"] + 1) if documents else 2
        doc = upload_pdf(
            session, pdf, sending_id=sending_id, postage_type=postage, priority=priority
        )
        if sending_id is None:
            sending_id = doc.get("sendingId")
        documents.append(doc)
        console.print(
            f"  → {pdf.name} ({doc.get('pagesCount')} page(s), id={doc.get('id', '?')[:8]})"
        )

    if not sending_id:
        raise click.ClickException("Upload succeeded but no sendingId in server response.")
    console.print(f"[dim]Draft id: {sending_id[:8]}…[/dim]")

    # 4. Sync draft and get the server-side prices.
    draft = build_draft(
        user_id=user_id,
        sending_id=sending_id,
        documents=documents,
        receivers=receivers,
        sender=sender,
        postage_type=postage,
        notice_of_receipt=ar,
        postal_tracking=tracking,
        duplex=duplex,
        color=color,
        deposit_date=deposit,
    )
    sync_draft(session, user_id, draft)
    # Push recipients before pricing — the server reads them when computing.
    recipient_id = push_recipients(
        session, receivers, sending_id=sending_id, postage_type=postage
    )

    # Compute pricing — this also populates server-side sheetsCount that
    # cart creation reads later (without it /carts/current 500s).
    price = calc_price(
        session,
        documents=documents,
        receivers=receivers,
        postage_type=postage,
        notice_of_receipt=ar,
        postal_tracking=tracking,
        duplex=duplex,
        color=color,
        deposit_date=deposit,
    )
    total = price.get("totalPrice") or {}
    console.print(
        f"[bold]Tarif :[/bold] [green]{total.get('formattedValue', '?')}[/green] "
        f"({total.get('value', '?')} {total.get('currencyIso', 'EUR')})"
    )

    if dry_run:
        console.print("[yellow]Dry run — cart not created.[/yellow]")
        return

    # 5. Materialize into cart (~5s, server generates the printable PDF).
    console.print("[dim]Creating cart entry (server is generating PDFs, ~5s)…[/dim]")
    cart = commit_to_cart(
        session,
        sending_id=sending_id,
        recipient_id=recipient_id,
        documents=documents,
        receivers=receivers,
        sender=sender,
        postage_type=postage,
        notice_of_receipt=ar,
        postal_tracking=tracking,
        duplex=duplex,
        color=color,
        deposit_date=deposit,
    )
    if cart:
        bundle = cart.get("celBundleId") or cart.get("code") or "?"
        console.print(f"[green]✓[/green] Cart entry created ({bundle}).")
    else:
        console.print("[green]✓[/green] Cart entry created.")

    # 6. Handoff to browser.
    console.print(f"\n[bold]Pay here:[/bold] [cyan]{HANDOFF_URL}[/cyan]")
    if open_browser:
        webbrowser.open(HANDOFF_URL)



@cli.command("auth-status")
@click.option("--json", "as_json", is_flag=True, help="Emit secret-free metadata.")
def auth_status(as_json):
    """Report credential storage without contacting the provider. Example: auth-status --json."""
    import json
    metadata = _auth_broker("status") or {
        "connector": "laposte", "account": "default", "source": "unavailable",
        "configured": False, "pending": False, "last_sync": None,
    }
    if not metadata.get("configured") and CONFIG_FILE.exists():
        metadata.update(source="legacy", configured=True)
    metadata.update(session_scope="portable")
    if CONFIG_FILE.with_suffix(".auth-pending").exists():
        metadata.update(source="pending-local", configured=True, pending=True)
    click.echo(json.dumps(metadata))


@cli.command("auth-sync")
def auth_sync():
    """Move stored credentials into 1Password. Example: auth-sync."""
    import json
    local_pending = CONFIG_FILE.with_suffix(".auth-pending")
    metadata = _auth_broker("save", _legacy_config()) if local_pending.exists() else _auth_broker("sync")
    if metadata and local_pending.exists():
        local_pending.unlink()
    if not metadata or not metadata.get("configured"):
        config = load_config()
        if config and not config.get("cookie_header"):
            header, user_id = get_browser_cookies(config.get("browser", "chrome"))
            config.update(cookie_header=header, userId=user_id)
        metadata = _auth_broker("save", config) if config else metadata
    click.echo(json.dumps(metadata or {"connector": "laposte", "source": "unavailable", "pending": False}))
    if not metadata or not metadata.get("configured") or metadata.get("pending"): raise click.exceptions.Exit(3)


if __name__ == "__main__":
    cli()
