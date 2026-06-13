# Seat Scanner

Watches AMC seat maps and sends a push notification to your phone when
the seats you want open up. Fully serverless:

- **GitHub Pages** serves a small PWA (this repo's `docs/`) where you pick
  showtimes, seats, and adjacency rules.
- **GitHub Actions** does all the AMC fetching (browsers can't — CORS) on a
  tiered schedule: every 6h when the show is >7 days out, every 30 min
  inside a week, every 15 min inside 24h, and every run (~5 min) in the
  last 4 hours.
- The **`data` branch** holds machine-written state (`state.json`,
  `seatmap-<id>.json`) so the code branch stays clean.
- Notifications are **Web Push** straight to the installed PWA — no
  third-party relay; the only moving part is a VAPID keypair.

## One-time setup

1. **Pages** — Settings → Pages → Deploy from a branch → select the default
   branch and `/docs`. The app will be at
   `https://<owner>.github.io/seat-scanner/`.
2. **VAPID secret** — Settings → Secrets and variables → Actions → New
   repository secret named `VAPID_PRIVATE_KEY`. The matching public key is
   already in `config.json`. (To rotate: generate a P-256 keypair, e.g.
   `npx web-push generate-vapid-keys`, update both halves.)
3. **Fine-grained PAT** — github.com → Settings → Developer settings →
   Fine-grained tokens → Generate new token. Scope it to **only this repo**
   with permissions **Contents: read and write** and **Actions: read and
   write**. Paste it into the app's Setup card; it is stored only in your
   browser's localStorage, never in the repo.
4. **Install the PWA (iPhone)** — open the Pages URL in Safari → Share →
   **Add to Home Screen** → open it from the home screen → tap **Enable
   notifications**. iOS only delivers web push to installed PWAs (iOS 16.4+).

## Using it

Paste an AMC showtime URL (the `/showtimes/<id>/seats` page) into "Add a
watch". The app asks Actions to fetch the live seat map, renders it, and
you tap the seats you care about (or watch whole rows / the whole room),
set how many adjacent seats you need, and save. You'll get at most one
push per distinct set of matching seats, and a "watcher broken" alert if
fetching fails 3 times in a row. Watches end automatically when the
showtime passes, and polling disables itself when nothing is left to watch
(saving your Actions minutes); it re-enables automatically when you add a
watch.

## How the AMC fetch works (re-capture notes)

The seat data is server-rendered into the page as Next.js flight chunks
(`self.__next_f.push`), guarded by Cloudflare and a Queue-It "Global
Safety Net" waiting room. Anonymous plain-HTTP works: request
`/showtimes/<id>/seats`, follow the cookie-test page's JS redirect into
`queue.amctheatres.com` with a cookie jar, get waved through, and parse
`"seatingLayout"` out of the flight payload (`scripts/amc.py`).

If AMC changes this, re-capture by opening a seats page with DevTools →
Network, and searching response bodies for a seat name like `A12`. Update
`amc.py`'s fetch hops and/or parsing to match; `scripts/probe.py` is a
standalone probe you can run from the `probe-seatmap` workflow to test
from a runner (push a new showtime ID to `probe-trigger.txt` or use the
Run workflow button).

Be gentle: the watcher makes exactly one page request per due check per
showing, with realistic browser headers.

## Security notes

- Public repo; nothing secret is committed. The PAT lives only in your
  browser. The VAPID private key lives only in an Actions secret.
- `subscriptions.json` contains your push endpoint; that's safe to publish
  because pushes also require the VAPID private key.
- Seat maps are public data; nothing personal is fetched or stored.
