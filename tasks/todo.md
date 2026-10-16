# Phone-only showtimes via an iOS Shortcut

AMC's Cloudflare blocks GitHub runners, but an iOS Shortcut's "Get Contents of
URL" from the phone gets the real page (user-verified: 1.1 MB). So the phone
fetches, Actions only parses.

- [x] amc.py: build a range listing from already-fetched pages (shared with
      fetch_showtimes_range)
- [x] fetch-showtimes.yml: optional `inbox` input → parse data/inbox/<id>.zip
      instead of fetching; delete the zip with the browse.json commit
- [x] app.js: Find showtimes → run Shortcut (urls + upload target) → wait for
      the zip → dispatch with inbox → existing browse.json wait
- [x] index.html: one-time Shortcut setup steps; remove the Safari bookmark UI
      and docs/grab.js
- [x] Tests: unit test for the new amc.py function; local run of the workflow
      script on a zip; Playwright run of the app flow with GitHub mocked

## Review
- 32 Python unit tests pass (2 new: range listing from pages; blocked page raises).
- Workflow script run locally on a Shortcut-style zip: out-of-order entries,
  names "1.html" / "2.html.html" / "10", __MACOSX junk → 3 pages in date
  order, browse.json written, zip removed; bad inbox id rejected.
- Playwright (GitHub API mocked): Find showtimes → shortcuts:// URL carries
  the 3 dated AMC URLs + upload URL + auth; app waits for the upload,
  dispatches with the same inbox id, renders browse.json; also resumes after
  a mid-Shortcut reload. No page errors.
- Not testable here: the Shortcut itself (built by hand on the phone) and
  GitHub accepting a multi-MB base64 PUT from it.
