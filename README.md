# Seat Scanner

Watches AMC seat maps and sends a push notification to your phone when
the seats you want open up. Fully serverless:

- **GitHub Pages** serves a small PWA (this repo's `docs/`) where you pick
  showtimes, seats, and adjacency rules.
- **GitHub Actions** does all the AMC fetching (browsers can't — CORS) at a
  flat ~30s cadence for every active watch, no matter how far off the
  showtime is: a jittered in-run burst on top of the 5-min cron, with runs
  self-chaining so a fresh run is always queued and coverage stays
  continuous. (This is deliberately aggressive; raise `CHECK_INTERVAL_MIN`
  in `scripts/check.py` to slow it down.)
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

**Browse shows (no URLs).** Paste your AMC theatre's page link once into
"Browse shows" (e.g.
`https://www.amctheatres.com/movie-theatres/boston/amc-boston-common-19`) —
it's remembered on this device. Pick a date, tap **Find showtimes**, and the
app lists every movie and showtime for that day. (Under the hood this asks
Actions to fetch the theatre's showtimes page and writes `browse.json` to
the `data` branch, which the app renders.)

**Bulk-watch (same criteria for many shows).** Tap any number of showtimes to
select them — selection persists as you change dates — then set the row /
adjacency / wheelchair-companion rules once and tap **Watch selected shows**
to create a watch for each. Because those watches share rules rather than
hand-picked seats, no seat maps need loading; the showtime IDs and times come
straight from the listing. To hand-pick exact seats instead, select a single
show and tap **Pick exact seats** (or use "Add a watch by link").

**Or add by link.** You can still paste an AMC showtime URL (the
`/showtimes/<id>/seats` page) or bare showtime ID into "Add a watch by link".

Either way, once the seat map renders you tap the seats you care about (or
watch whole rows / the whole room), set how many adjacent seats you need, and
save. You'll get at most one push per distinct set of matching seats, and a
"watcher broken" alert if fetching fails 3 times in a row. Watches end
automatically when the showtime passes, and polling disables itself when
nothing is left to watch (saving your Actions minutes); it re-enables
automatically when you add a watch.

## How the AMC fetch works (re-capture notes)

The seat data is server-rendered into the page as Next.js flight chunks
(`self.__next_f.push`), guarded by Cloudflare and a Queue-It "Global
Safety Net" waiting room. Anonymous plain-HTTP works: request
`/showtimes/<id>/seats`, follow the cookie-test page's JS redirect into
`queue.amctheatres.com` with a cookie jar, get waved through, and parse
`"seatingLayout"` out of the flight payload (`scripts/amc.py`).

The **browse** feature fetches the same way from
`/movie-theatres/<market>/<slug>/showtimes?date=<YYYY-MM-DD>`, but that page
renders showtimes as React server-component markup rather than clean JSON, so
`parse_showtimes` keys off three stable shapes in the flight: format-group
headings (`"id":"<movie-slug>-<theatre-slug>-<format>-<n>"`), showtime objects
(`{"showtimeId":…,"showDateTimeUtc":…,"display":{"time":…}}`), and movie
link/title pairs. Each showtime is grouped under whichever heading last
preceded it. If AMC changes this markup, re-capture as above and adjust those
three patterns.

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
