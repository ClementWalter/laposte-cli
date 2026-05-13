#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "click>=8.1",
#     "curl_cffi>=0.7",
#     "rich>=13.0",
# ]
# ///
"""La Poste CLI — send registered (LREL) or priority (LEL) mail from the terminal.

Drives the laposte.fr "Courrier En Ligne" (CEL) flow up to the payment page,
then hands off to your browser to confirm payment with a saved card. PDF only.

Auth is cookie-based: paste your laposte.fr browser cookies once via `login`
and the CLI replays them on every CEL API call. Cookies live in
~/.config/laposte-cli/config.json (mode 600).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import sys
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlencode

import click
from curl_cffi import requests
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


# --- Config ------------------------------------------------------------------


def load_config() -> dict:
    """Return stored config, or empty dict if no login yet."""
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def save_config(config: dict) -> None:
    """Persist config; chmod 600 since it holds session cookies."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    CONFIG_FILE.chmod(0o600)


def require_login() -> dict:
    """Return loaded config, or raise a clean CLI error if not logged in."""
    cfg = load_config()
    if not cfg.get("cookies") or not cfg.get("userId"):
        raise click.ClickException("Not logged in. Run 'laposte-cli login' first.")
    return cfg


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


def make_session(cookie_header: str) -> requests.Session:
    """Build a curl_cffi session impersonating Chrome with our stored cookies."""
    s = requests.Session(impersonate=TLS_IMPERSONATE)
    s.headers.update(
        {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
            "Origin": BASE,
            "Referer": f"{BASE}/envoi-courrier-en-ligne/parcours/creer-lettre",
            "Cookie": cookie_header,
        }
    )
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
) -> dict:
    """Ask La Poste's SERCADIA to RNVP-certify an address and return the ceaid block."""
    params = {"streetName": street, "place": f"{zip_code} {city}"}
    if additional:
        params["additionalStreetName"] = additional
    r = session.get(SERCADIA_URL, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


# --- Draft helpers -----------------------------------------------------------


def today_dmy() -> str:
    """Today's date in La Poste's DD/MM/YYYY format."""
    return _dt.date.today().strftime("%d/%m/%Y")


def fetch_sender_addresses(session: requests.Session) -> list[dict]:
    """Return the user's saved sender (postal) addresses."""
    r = session.get(ADDRESSES_URL, timeout=15)
    r.raise_for_status()
    data = r.json()
    # Shape may be {postalAddresses: [...]} or {addresses: [...]} — try both.
    return data.get("postalAddresses") or data.get("addresses") or []


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
    sending_id: str,
    postage_type: str,
    priority: int,
) -> dict:
    """Upload one PDF and return the document descriptor returned by the server."""
    pages_count = _pdf_page_count(pdf_path)
    size = pdf_path.stat().st_size
    fields = {
        "name": pdf_path.name,
        "fileDocument": (pdf_path.name, pdf_path.read_bytes(), "application/pdf"),
        "documentPriority": str(priority),
        "pagesCount": str(pages_count),
        "postageType": postage_type,
        "source": "upload",
        "totalSize": str(size),
        "sendingId": sending_id,
        "frontPostageType": postage_type,
    }
    r = session.post(UPLOAD_URL, files=fields, timeout=120)
    if not r.ok:
        raise click.ClickException(f"Upload failed for {pdf_path.name}: {r.status_code} {r.text[:300]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


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


def commit_to_cart(session: requests.Session) -> dict | None:
    """Tell the server to materialize the current draft into a cart bundle.

    Sequence observed in the browser: /recipients then /options then /carts/current.
    The bodies are not yet reverse-engineered; we try empty POSTs and surface
    any error so we can iterate.
    """
    for url in (RECIPIENTS_URL, OPTIONS_URL):
        r = session.post(url, json={}, timeout=30)
        logger.debug("POST %s -> %s %s", url, r.status_code, r.text[:200])
        if not r.ok:
            console.print(f"[yellow]Warning:[/yellow] {url} returned {r.status_code}: {r.text[:200]}")

    r = session.post(CART_CREATE_URL, json={}, timeout=60)
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
def login() -> None:
    """Save your laposte.fr browser cookies.

    Steps:
      1. Log in to https://www.laposte.fr in your browser.
      2. Open DevTools → Network → click any request to www.laposte.fr →
         right-click → Copy → Copy as cURL.
      3. Paste the curl here. We only keep the `Cookie:` header.
    """
    console.print(
        "[bold]Steps:[/bold]\n"
        "  1. Log in to [cyan]https://www.laposte.fr[/cyan]\n"
        "  2. DevTools → Network → any request to laposte.fr → "
        "right-click → Copy → Copy as cURL\n"
        "  3. Paste below. Empty line ends input.\n"
    )
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if not line.strip() and lines:
            break
        lines.append(line)
    blob = "\n".join(lines)

    cookie_header = _extract_cookie_from_blob(blob)
    if not cookie_header:
        raise click.ClickException(
            "No 'Cookie:' header found in input. Paste a full curl command, "
            "or just the cookie header value."
        )
    cookies = parse_cookie_header(cookie_header)
    user_id = extract_user_id(cookies)
    if not user_id:
        raise click.ClickException(
            "Could not find pa_user cookie. Make sure you're logged in to "
            "laposte.fr before copying the curl."
        )

    s = make_session(cookie_header)
    try:
        r = s.get(PING_URL, timeout=10)
        if not r.ok:
            console.print(f"[yellow]Warning:[/yellow] ping returned {r.status_code}")
    except Exception as exc:
        console.print(f"[yellow]Warning:[/yellow] could not ping La Poste: {exc}")

    save_config({"cookies": cookie_header, "userId": user_id})
    console.print(f"[green]✓[/green] Logged in as user [cyan]{user_id}[/cyan]")
    console.print(f"  Cookies saved to {CONFIG_FILE} (mode 0600)")


def _extract_cookie_from_blob(blob: str) -> str | None:
    """Pull the Cookie header out of a pasted curl command or raw string."""
    # Look for `-b 'xxx'` or `-b "xxx"` or `--cookie 'xxx'`
    import re

    m = re.search(r"(?:-b|--cookie)\s+(['\"])(.+?)\1", blob, flags=re.DOTALL)
    if m:
        return m.group(2).strip()
    # Look for `Cookie: xxx` header (e.g. from devtools "copy as fetch")
    m = re.search(r"[Cc]ookie:\s*(.+?)(?:\n|$)", blob)
    if m:
        return m.group(1).strip()
    # Fall back: if the whole blob looks like a cookie header, use it.
    if "=" in blob and ";" in blob:
        return blob.strip()
    return None


@cli.command()
def whoami() -> None:
    """Show the user id of the saved session."""
    cfg = require_login()
    console.print(f"User ID: [cyan]{cfg['userId']}[/cyan]")
    console.print(f"Config:  {CONFIG_FILE}")


@cli.command()
def logout() -> None:
    """Delete the saved cookies."""
    if CONFIG_FILE.exists():
        CONFIG_FILE.unlink()
        console.print("[green]✓[/green] Logged out.")
    else:
        console.print("Already logged out.")


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

    # 3. Upload PDFs. We re-use a random-ish sendingId (UUID) per session.
    import uuid

    sending_id = str(uuid.uuid4())
    console.print(f"[dim]Uploading {len(pdfs)} PDF(s)…[/dim]")
    documents: list[dict] = []
    for i, pdf in enumerate(pdfs, start=1):
        doc = upload_pdf(
            session, pdf, sending_id=sending_id, postage_type=postage, priority=i
        )
        documents.append(doc)
        pages = doc.get("pagesCount") or _pdf_page_count(pdf)
        console.print(f"  → {pdf.name} ({pages} page(s))")

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
    echoed = sync_draft(session, user_id, draft)
    total = (echoed.get("options") or {}).get("totalPrice") or {}
    console.print(
        f"[bold]Tarif :[/bold] [green]{total.get('formattedValue', '?')}[/green] "
        f"({total.get('value', '?')} {total.get('currencyIso', 'EUR')})"
    )

    if dry_run:
        console.print("[yellow]Dry run — cart not created.[/yellow]")
        return

    # 5. Materialize into cart.
    console.print("[dim]Creating cart entry…[/dim]")
    cart = commit_to_cart(session)
    if cart:
        bundle = cart.get("celBundleId") or cart.get("code") or "?"
        console.print(f"[green]✓[/green] Cart entry created ({bundle}).")
    else:
        console.print("[green]✓[/green] Cart entry created.")

    # 6. Handoff to browser.
    console.print(f"\n[bold]Pay here:[/bold] [cyan]{HANDOFF_URL}[/cyan]")
    if open_browser:
        webbrowser.open(HANDOFF_URL)


if __name__ == "__main__":
    cli()
