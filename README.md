# laposte-cli

Send registered (LREL) or priority (LEL) mail from your terminal via
[laposte.fr / Courrier En Ligne](https://www.laposte.fr/envoi-courrier-en-ligne).

The CLI drives the same back-end the website uses (CEL API) and prepares
everything up to the payment page. It then hands off to your browser so you
can pay with a saved card (and complete 3DS if your bank prompts for it) —
the CLI never touches your CB.

> Status: V1 (handoff payment) — working end-to-end against the live API
> as of 2026-05-13. PDF upload only — text-editor mode is currently broken
> on laposte.fr's side anyway. Tested on macOS with `uv` + Chrome.

## Install

Requires `uv` and Python 3.11+. No `pip install` needed — the script
declares its dependencies inline via [PEP 723](https://peps.python.org/pep-0723/).

```bash
git clone https://github.com/ClementWalter/laposte-cli.git
cd laposte-cli
./laposte_cli.py --help
```

Or add to your PATH:

```bash
ln -s "$PWD/laposte_cli.py" ~/.local/bin/laposte-cli
```

## Login (no copy-paste)

The CLI reuses your existing laposte.fr browser session by reading cookies
directly from your browser's local store via
[`browser_cookie3`](https://github.com/borisbabic/browser_cookie3).

```bash
laposte-cli login                  # defaults to chrome
laposte-cli login --browser firefox
```

`login` validates access to La Poste before saving the session. Failed
validation keeps the existing login intact. When vault synchronization is
unavailable, the validated session is saved locally and the command reports
that synchronization is pending.

On macOS Chrome, the first read pops a Keychain prompt to allow access to
Chrome's encrypted cookie file — accept once and you're done. The CLI stores
the browser preference and session in `~/.config/laposte-cli/config.json`
with owner-only permissions and synchronizes them through the vault broker.
Cookies are re-read live for browser-configured logins on each command.

Supported browsers: `chrome`, `firefox`, `safari`, `edge`, `brave`,
`chromium`, `opera`, `arc`.

Verify:

```bash
laposte-cli whoami       # verifies access and shows the account
laposte-cli addresses    # prints saved sender postal addresses
```

All three authentication commands support `--json`:

```bash
laposte login --json
laposte whoami --json
laposte logout --json
```

`logout` removes the CLI's local credentials and keeps this device logged out
until an explicit `login` succeeds, even if browser or vault credentials are
still available. It does not sign out the website or revoke the shared vault
session on other devices. Repeating `logout` is safe.

Browser-configured logins use current browser cookies for the same account,
with the saved session as a fallback when that browser login is unavailable.
Shared sessions without a browser preference use their saved cookies directly.

La Poste's service-unavailable page can also appear when it rejects a stale
CLI session. It does not establish an outage. Log in on laposte.fr and open
Courrier en ligne before retrying `laposte addresses`.

## Send a letter

```bash
laposte-cli send invoice.pdf \
    --to "M Jean Dupont|1 rue de la Paix|75001 PARIS" \
    --format recommande --ar
```

This:

1. Uploads the PDF(s) to La Poste.
2. RNVP-certifies the recipient(s) via the SERCADIA endpoint.
3. Syncs the draft and shows the computed price.
4. Creates the cart entry.
5. Opens https://www.laposte.fr/checkout/recapitulatif in your browser
   so you can confirm payment with a saved card.

### Options

| Flag | Default | Meaning |
|---|---|---|
| `--to "M Prenom Nom\|street\|cp ville"` | required | Recipient. Repeat for multiple. |
| `--format` | `recommande` | `recommande` (LREL) or `lettre-rouge` (LEL). |
| `--ar / --no-ar` | `--no-ar` | Avis de réception (+1,25 €, LREL only). |
| `--tracking / --no-tracking` | `--no-tracking` | Postal tracking (LEL only, +0,50 €). |
| `--color / --bw` | `--bw` | Color printing (+0,50 €). |
| `--duplex / --simplex` | `--duplex` | Recto-verso (default) or recto only. |
| `--from <id-or-label>` | primary | Sender address — id or label from `addresses`. |
| `--no-sender` | off | Do not include a return address. |
| `--date DD/MM/YYYY` | today | Deposit date. |
| `--dry-run` | off | Upload + show price only — don't create the cart. |
| `--no-open` | off | Don't open the browser at the end. |

### What "handoff" means

The CLI stops one click short of the actual payment. You'll land on a
page with your saved cards listed; click *Payer* and your bank handles 3DS
as usual. The total takes 5 seconds at most.

The CLI **does not** and will not store, transmit, or read your card
data — payment lives entirely in your browser.

### Output you can expect

```
$ laposte-cli send acte.pdf --to "M Recipient Name|<street>|<cp> <city>" --format recommande --ar
From: <label> — <street> <cp> <city>
Resolving 1 recipient(s)…
  → M RECIPIENT NAME — <street> <cp> <city> (ceaid=<10-char-RNVP-code>)
Uploading 1 PDF(s)…
  → acte.pdf (4 page(s), id=<doc-uuid>)
Draft id: <sending-uuid>…
Tarif : 8,60 € (8.596 EUR)
Creating cart entry (server is generating PDFs, ~5s)…
✓ Cart entry created (?).

Pay here: https://www.laposte.fr/checkout/recapitulatif
```

## Why not text-editor mode?

The website's "Rédiger un texte" flow appears to be broken (the
*Valider ce document* button doesn't trigger any back-end call as of
2026-05-13). When/if it gets fixed we'll add a `--text body.md` option.

## How it works (TL;DR for hackers)

The CLI mirrors what the Nuxt SPA at
`/envoi-courrier-en-ligne/parcours/creer-lettre` does, in this order:

| Step | Endpoint |
|---|---|
| Read cookies | `browser_cookie3.chrome(domain_name="laposte.fr")` (includes Keycloak SSO cookies, HttpOnly ones too) |
| Keep-alive | `GET /cel/api/ping` (refreshes the session token before TS\* cookies rotate) |
| Sender addresses | `GET /cel/address/lpelPart/account/profiles/CURRENT/postal-addresses` (dict keyed by postalId) |
| RNVP cert recipient | `GET /cel/address/sercadia/check?streetName=&place={CP}+{VILLE}` (returns `[{ceaid, ...}]`) |
| Wipe old draft | `DELETE /cel/sending/lpelPart/{userId}` (idempotent) |
| Create + upload | `POST /cel/.../e-service/cel/upload` (multipart, **no sendingId on first call** — server allocates one) |
| Sync state | `POST /cel/sending/lpelPart/{userId}` (full draft JSON) |
| Push recipients | `POST /cel/.../e-service/cel/recipients` body `{addresses, sendingId, postageType}` (returns `recipients[0].id`) |
| Compute price | `POST /cel/.../e-service/cel/price` (celConfigurationWsDTO with sheetsCount) |
| Materialize cart | `POST /cel/.../e-service/cel/users/current/carts/current` (addEServiceToCart item) |
| Pay | User clicks at `https://www.laposte.fr/checkout/recapitulatif` |

The address-shape gotcha: `/sending` uses `city/zipCode/country=FR` flat,
while `/recipients` + `/price` + cart use `town/postalCode/country={isocode:"FR"}`
nested. We translate between the two with `_to_api_address()`.

The sendingId gotcha: it's a server-issued UUID that lives in a separate
upload-tracking store. Posting a client-generated UUID via `/sending` is
echoed back but **doesn't register** it for `/upload` — the first upload
must omit the field and read the allocated UUID from the response.

## Roadmap

- **V1** (this) — handoff payment.
- **V2** — try to drive Scellius (`scelliuspaiement.labanquepostale.fr`)
  for `--auto-pay` with a saved card. Will only work for amounts low
  enough to trigger frictionless 3DS.
- **V3** — `addresses add`, `templates`, history listing.

## License

MIT.

## Disclaimer

Not affiliated with La Poste. Use at your own risk. The CLI re-uses your
own session and only does things you could do in the browser yourself —
but if La Poste changes its API tomorrow, the CLI will break, and you
might burn a few cents on a failed test letter.
