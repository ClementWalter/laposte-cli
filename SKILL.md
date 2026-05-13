---
name: laposte-cli
description: Send registered (LREL) or priority (LEL) mail via laposte.fr from the terminal using laposte_cli. Use when the user wants to send a paper letter, recommandé/AR, e-lettre rouge, or upload a PDF for La Poste to print + distribute. The CLI uploads PDF(s), RNVP-certifies recipient address(es), creates a cart entry, and opens the payment page in the browser for the user to confirm with a saved card. Every command supports --debug for HTTP traces.
---

# laposte-cli

CLI on top of the laposte.fr "Courrier En Ligne" (CEL) back-end. PDF-only.
Handoff payment: the CLI never sees the user's card — it opens
`https://www.laposte.fr/checkout/recapitulatif` at the end so the user can
pay with a saved card in the browser (3DS happens there as usual).

## When to use

- "envoie cette lettre en recommandé à X"
- "fais-moi un envoi de courrier postal pour ces 3 destinataires"
- "génère le recommandé AR pour ce PDF"
- "renvoie ce document à mon notaire avec suivi"

## When NOT to use

- The user wants the *text editor* mode (write the body in the browser).
  The website's editor flow is broken as of 2026-05-13 — use a separate
  tool to render text to PDF first, then upload it.
- The user wants to pay programmatically. The CLI stops at the payment
  page on purpose (V1 = handoff). A future V2 will try Scellius
  automation with saved cards, but it isn't implemented yet.

## Usage

### One-time login

```bash
laposte-cli login
```

Prompts the user to paste a `Copy as cURL` from DevTools while logged
into laposte.fr. The CLI extracts the `Cookie:` header and stores it in
`~/.config/laposte-cli/config.json` (mode 600). The `pa_user` cookie
contains the numeric `userId` we need for all subsequent CEL calls.

If `whoami` fails with "Not logged in", run `login` again — cookies may
have expired (typically lasts a few weeks).

### Send

```bash
laposte-cli send <pdf> [<pdf>...] --to "M Prenom Nom|street|cp ville" [options]
```

Required:
- One or more PDF paths (positional, repeatable). Max 20 Mo per PDF.
- `--to "Civ Prenom Nom|street|cp ville"`. Repeat for multiple recipients
  (up to 100). The `|` is the field separator. Civility is `M` or `MME`.

Format / options:
- `--format recommande|lettre-rouge` — LREL (default, J+3) or LEL (J+1).
- `--ar` — Avis de réception (recommandé only, +1,25 €).
- `--tracking` — Postal tracking (e-lettre rouge only, +0,50 €).
- `--color` / `--bw` — Color print (+0,50 €) or B&W (default).
- `--duplex` / `--simplex` — Recto-verso (default) or recto only.
- `--from <id-or-label>` — Sender address; defaults to the primary one.
  See `laposte-cli addresses` for the list.
- `--no-sender` — Send with no return address.
- `--date DD/MM/YYYY` — Deposit date. Default: today.
- `--dry-run` — Upload + price only, don't create the cart.
- `--no-open` — Don't open the browser at the end (still prints the URL).

### Inspect

- `laposte-cli whoami` — show the logged-in userId.
- `laposte-cli addresses` — list saved sender addresses.

## Architecture notes (for future maintainers)

### Auth
Cookie-based, no API key. The CLI replays the raw `Cookie:` header on every
request. WAF in front of laposte.fr does TLS fingerprinting, so we use
`curl_cffi` with `impersonate="chrome131"`.

### Address flow
1. Free-text recipients are RNVP-certified via
   `GET /cel/address/sercadia/check?streetName=…&place=CP+VILLE`.
   The response includes the `ceaid` (La Poste's address code) and the
   `rnvpValidation: "verified"` field that the cart creation needs.
2. The optional `--from` looks up
   `GET /cel/address/lpelPart/account/profiles/CURRENT/postal-addresses`
   for the sender's saved addresses.

### Draft
The SPA uses a *single* mutable draft state object keyed by `userId`,
synced via `POST /cel/sending/lpelPart/{userId}` with the full JSON
payload on every change. The server echoes the same shape back with
computed prices. We build that object once and POST it once.

### Cart
Materializing the draft into a real cart is a 3-step server-side dance:
`POST /cel/.../e-service/cel/recipients`, then `/options`, then
`/users/current/carts/current`. V1 sends empty JSON for all three —
seems to work because the server reads state from the draft, but if it
turns out the bodies are needed we'll fix that.

### Payment
Out of scope. The handoff URL is hardcoded
(`https://www.laposte.fr/checkout/recapitulatif`). The cookie
`lpel_cart={code, guid}` is set by the server after cart creation and
travels with the browser session.

## Error handling

- 401/403 from any La Poste endpoint → cookies expired, re-run `login`.
- 503 Backend fetch failed (varnish) → transient, retry.
- SERCADIA returns no result → the address isn't RNVP-validatable; try a
  different street/postal code.
- Upload returns non-200 → PDF too large or wrong format. Limit is 20 Mo
  per file, PDF/JPG/PNG only.

## Debug

`laposte-cli --debug send …` enables verbose HTTP logging via stdlib
`logging`. Combine with `curl_cffi`'s built-in tracing if you need
the wire details.
