# Browse showtimes from the phone (AMC blocks GitHub runners)

AMC's Cloudflare 403s fetch-showtimes on GitHub runners since 2026-09-05.
Workaround: the user's own browser (which passes Cloudflare normally) reads
the showtimes and hands them to the app.

- [x] Fail fast in the UI when a dispatched run fails (was: spin to timeout)
- [x] docs/grab.js: bookmarklet body — on an AMC theatre page, fetch N days
      same-origin, parse flight data (port of amc.parse_showtimes/merge), open
      the app with `#import=<listing>`
- [x] app.js: consume `#import=` on load → render the showlist as if browsed
- [x] index.html: "Open on AMC" button + setup steps + copy-bookmark button
- [x] Differential test: JS parser vs Python parser on the test fixture
- [x] Push; report untestable bits (no AMC access from the sandbox)

## Review
- Differential test: JS parser output == Python parse_showtimes/merge_showtimes
  on the test fixture (single day, 2 days with a duplicate day, chunked flight).
- Playwright e2e with amctheatres.com intercepted to serve the fixture: copy
  bookmark → Open on AMC → run bookmark, answer "2" days → app opens with both
  days rendered, theatre/date restored, selection + bulk bar work, no errors.
- Not verifiable here (sandbox can't reach AMC): real AMC markup today, and
  whether AMC's CSP/Safari let the bookmark run. Seat-page fetches from Actions
  (polling) are untested since 2026-09-05 — no active watches since then.
