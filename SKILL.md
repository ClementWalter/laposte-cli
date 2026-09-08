---
name: laposte-cli
description: Send registered (LREL) or priority (LEL) French postal mail via laposte.fr from the terminal using `laposte_cli`. Use when the user wants to send a paper letter, a recommandé/AR, an e-lettre rouge, or upload one or more PDFs for La Poste to print + distribute. The CLI uploads PDF(s), RNVP-certifies recipient address(es), creates a cart entry, then opens https://www.laposte.fr/checkout/recapitulatif so the user can confirm payment in the browser with a saved card. Cookies are read live from the user's browser — no manual paste, no API key.
---

# laposte-cli

CLI on top of laposte.fr's "Courrier En Ligne" (CEL) back-end. PDF only.
**Handoff payment** — the CLI never sees or transmits card data; it stops
at the payment page and the user clicks *Payer* themselves.

## How to invoke

Invoke it as **`laposte`** — on `$PATH` via a symlink in `~/.local/bin` onto this
repo's `bin/laposte`, so it always runs the current checkout: a `git pull`, or even
an uncommitted edit, takes effect immediately with nothing to reinstall.

```bash
laposte whoami
```

Examples in this doc are written that way. If `laposte` is not on `$PATH`, run the
bundled launcher `bin/laposte` resolved against this skill's own directory (PEP 723
— `uv` resolves deps inline on first run), or link it once:

```bash
ln -sfn <skill-dir>/bin/laposte ~/.local/bin/laposte
```

## When to use

- "envoie cette lettre en recommandé à X"
- "fais-moi un envoi postal de ces 3 PDF pour mon notaire"
- "génère un recommandé avec AR pour ce courrier"
- "envoie ça en e-lettre rouge demain"

## When NOT to use

- The user has only text (no PDF). The website's "Rédiger un texte" mode
  is broken as of 2026-05-13 and the CLI doesn't support it. Render the
  text to PDF first (e.g. via pandoc, libreoffice, or `weasyprint`),
  then upload.
- The user wants the CLI to pay automatically. V1 is handoff only —
  payment lives in the browser. Do not attempt to bypass this.

## Usage by an agent

### 1. Verify login state

Before anything, run:

```bash
laposte-cli whoami
```

If it returns a `User ID:` line, the CLI has verified access with La Poste.
If it errors with "Not logged in",
the user needs to:

1. Open https://www.laposte.fr in Chrome (or another supported browser:
   firefox/safari/edge/brave/chromium/opera/arc), log in, and open Courrier en ligne.
2. Run `laposte-cli login --browser <name>` once. On macOS Chrome the
   first read may pop a Keychain prompt — the user accepts it once.

`login` validates the session before saving it. `whoami` performs a live
check; `auth-status` only reports credential storage. `login`, `logout`, and
`whoami` support `--json` without exposing session cookies.

`laposte logout` clears local credentials and prevents automatic login from
the browser or vault until an explicit `laposte login` succeeds. Logout is
local to this device; it does not revoke the browser or shared vault session.

### 2. List sender addresses (optional, for `--from`)

```bash
laposte-cli addresses
```

Prints a table of the user's saved postal addresses with their IDs and
labels. Useful for picking `--from <label>` (otherwise the primary
address is used).

### 3. Send

```bash
laposte-cli send <pdf> [<pdf>...] --to "Civ Prenom Nom|street|CP VILLE" [options]
```

Required:
- One or more PDF paths (positional, repeatable). Max 20 Mo per PDF.
- One or more `--to` flags. The pipe `|` is the field separator. Format:
  `"M Jean Dupont|1 rue de la Paix|75001 PARIS"`. Civility is `M` or
  `MME` (defaults to `M` if missing). Up to 100 recipients per letter.

Format / options:
- `--format recommande` (default, LREL) | `lettre-rouge` (LEL, J+1).
- `--ar` — *avis de réception* (LREL only, +1,25 €).
- `--tracking` — postal tracking (LEL only, +0,50 €).
- `--color` / `--bw` — color print (+0,50 €) or B&W (default).
- `--duplex` / `--simplex` — recto-verso (default) or recto only.
- `--from <id-or-label>` — sender address, default = primary.
- `--no-sender` — omit the return address.
- `--date DD/MM/YYYY` — deposit date, default = today.
- `--dry-run` — upload + price only, don't create the cart.
- `--no-open` — print the checkout URL but don't open it in a browser.

### Example: registered letter with AR (recipient provided by the user)

```bash
laposte-cli send acte.pdf annexes.pdf \
  --to "<civility> <First> <Last>|<street>|<cp> <city>" \
  --format recommande --ar
```

### Example: e-lettre rouge with tracking, batch send

```bash
laposte-cli send invoice.pdf \
  --to "<civility> <First> <Last>|<street>|<cp> <city>" \
  --to "<civility> <First> <Last>|<street>|<cp> <city>" \
  --format lettre-rouge --tracking
```

## Expected output (success path)

```
From: <label> — <street> <cp> <city>
Resolving 1 recipient(s)…
  → <FULL NAME> — <street> <cp> <city> (ceaid=<10-char-RNVP-code>)
Uploading 1 PDF(s)…
  → acte.pdf (4 page(s), id=<doc-uuid>)
Draft id: <sending-uuid>…
Tarif : 8,60 € (8.596 EUR)
Creating cart entry (server is generating PDFs, ~5s)…
✓ Cart entry created (?).

Pay here: https://www.laposte.fr/checkout/recapitulatif
```

After this the user must visit (or have their browser opened to) that URL
and click *Payer* with one of their saved cards. The CLI's job is done.

## Failure modes

| Symptom | What it means | Action |
|---|---|---|
| `Not logged in` | No supported browser session found | Tell user to log in to laposte.fr in their browser, then `laposte-cli login --browser <name>` |
| `SERCADIA could not RNVP-certify` | The recipient address is not in La Poste's database | Recheck the spelling; try with the official postal address |
| `Price calc failed: 500 ... sheetsCount null` | We forgot to populate page metrics | Bug — file an issue. Should not happen for PDF input. |
| `Cart creation failed: 400 ... recipientId` | /recipients endpoint refused our shape | Re-check the `--to` syntax; report the address that failed |
| HTTP 503 *Backend fetch failed* (Varnish) | Transient La Poste hiccup | Just retry |
| `Upload failed: 400 ... SENDING_NOT_FOUND` | sendingId out of sync | Should self-recover; re-run |

## After sending

The cart sticks around for ~24h in `https://www.laposte.fr/checkout/recapitulatif`.
If the user wants to cancel a queued cart, they can empty it in the browser
(no CLI command for that yet — V3 territory).

## Architecture (for maintenance)

| Step | Endpoint |
|---|---|
| Auth | `browser_cookie3.chrome(domain_name="laposte.fr")` — read live every call |
| Sender list | `GET /cel/address/lpelPart/account/profiles/CURRENT/postal-addresses` |
| RNVP cert | `GET /cel/address/sercadia/check?streetName=&place={CP}+{VILLE}` |
| Wipe draft | `DELETE /cel/sending/lpelPart/{userId}` |
| Upload | `POST /cel/occ/.../e-service/cel/upload` (multipart, **omit sendingId** on first call) |
| Sync state | `POST /cel/sending/lpelPart/{userId}` (full draft JSON) |
| Recipients | `POST /cel/occ/.../e-service/cel/recipients` `{addresses, sendingId, postageType}` |
| Price | `POST /cel/occ/.../e-service/cel/price` (celConfigurationWsDTO + sheetsCount) |
| Cart | `POST /cel/occ/.../e-service/cel/users/current/carts/current` (addEServiceToCart item) |

Gotchas worth knowing:
- The CEL API uses postal *line-number* address naming (`line1` = street,
  `town`, `postalCode`, `country: {isocode}`, `ceaId` capital I,
  `titleCode: "mr"/"mrs"` + `title: "M."/"Mme"`). The translation lives in
  `_to_api_address()` in the script.
- `lpel_ftid_cel_part` / `lpel_ftid_chkt_part` / `lpel_ftid_ecom_part`
  share a UUID — only the prefix differs.
- The `lpel_cel` cookie tracks the *active* server-side draft; the CLI
  ignores it and lets `/upload` allocate a fresh one each run.

## Don't

- Don't try to also drive the payment from the CLI. Tell the user the
  payment is in their browser.
- Don't paste cookies for the user from logs/transcripts; cookies rotate
  and they're in the user's browser already.
- Don't reuse old `sendingId` across runs — let the server allocate.
