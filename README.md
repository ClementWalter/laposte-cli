# laposte-cli

Send registered (LREL) or priority (LEL) mail from your terminal via
[laposte.fr / Courrier En Ligne](https://www.laposte.fr/envoi-courrier-en-ligne).

The CLI drives the same back-end the website uses (CEL API) and prepares
everything up to the payment page. It then hands off to your browser so you
can pay with a saved card (and complete 3DS if your bank prompts for it) —
the CLI never touches your CB.

> Status: V1 (handoff payment). PDF upload only — text-editor mode is
> currently broken on laposte.fr's side anyway. Tested on macOS with `uv`.

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

## Login (paste cookies)

The CLI reuses your existing laposte.fr browser session. There's no OAuth
flow exposed by La Poste, so we copy cookies from DevTools once.

```bash
laposte-cli login
```

Then:

1. Log in to https://www.laposte.fr in your browser.
2. Open DevTools → **Network** → click on any request to `www.laposte.fr` →
   right-click → **Copy → Copy as cURL**.
3. Paste it into the prompt (multi-line OK; finish with an empty line).
4. The CLI extracts the `Cookie:` header and stores it in
   `~/.config/laposte-cli/config.json` (mode `0600`).

Verify:

```bash
laposte-cli whoami
laposte-cli addresses
```

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

## Why not text-editor mode?

The website's "Rédiger un texte" flow appears to be broken (the
*Valider ce document* button doesn't trigger any back-end call as of
2026-05-13). When/if it gets fixed we'll add a `--text body.md` option.

## How it works (TL;DR for hackers)

| Endpoint | Used for |
|---|---|
| `POST /cel/.../e-service/cel/upload` (multipart) | Upload a PDF, returns a doc descriptor with `id`. |
| `POST /cel/sending/lpelPart/{userId}` | Sync the full draft (options + content + addresses). Server echoes prices. |
| `GET /cel/address/sercadia/check?streetName=&place=` | RNVP-certify an address, returns the `ceaid` (La Poste's address code). |
| `GET /cel/address/lpelPart/account/profiles/CURRENT/postal-addresses` | List the user's saved sender addresses. |
| `POST /cel/.../e-service/cel/recipients` + `/options` + `/users/current/carts/current` | Materialize the draft into a cart bundle. |
| `https://data.geopf.fr/geocodage/search` (public) | Address autocomplete (BAN/IGN). Used for free-text recipients. |

The full cart-creation payload (`POST .../users/current/carts/current`)
hasn't been fully reverse-engineered yet — V1 sends empty JSON and relies
on the server picking up state from the prior `/sending` sync. If the
payload turns out to be required, we'll fix it in V1.1.

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
