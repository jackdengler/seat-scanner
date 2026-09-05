#!/usr/bin/env python3
"""Standalone probe: fetch one AMC seat map and print the parsed seats.

Usage:
    python3 probe.py <showtimeId>            # seat map: fetch, parse, print
    python3 probe.py <market>/<slug>[@date]  # theatre showtimes for a date
    python3 probe.py <path-to-html>          # parse a saved file (offline test)

Fetching and parsing come straight from amc.py, so what the probe sees is
exactly what the watcher sees — including the homepage warm-up, the
Queue-It redirect chain, and the retries on a Cloudflare block. Run it from
the probe-seatmap workflow to reproduce a runner-side block.
"""

import datetime
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amc


def fetch(showtime_id):
    try:
        return amc.fetch_html(showtime_id, log=print)
    except amc.FetchBlocked as e:
        print(f"FETCH FAILED: {e.diagnosis}")
        diagnose(e.body)
        sys.exit(1)
    except Exception as e:  # noqa: BLE001 — a probe reports, it doesn't crash
        print(f"FETCH FAILED: {type(e).__name__}: {e}")
        sys.exit(1)


DIAGNOSES = {
    "cloudflare-challenge": "Cloudflare challenge page (bot check).",
    "queue-it-waiting-room": "Queue-It virtual waiting room redirect.",
    "access-denied": "Access denied / blocked.",
}


def diagnose(body):
    """Print which bot-protection layer we hit, if recognizable."""
    known = DIAGNOSES.get(amc.diagnose(body or ""))
    if known:
        print("DIAGNOSIS:", known)
    else:
        print("DIAGNOSIS: unrecognized failure. First 500 chars of body:")
        print((body or "")[:500])


def parse(html):
    try:
        seatmap = amc.parse_seatmap(html)
    except amc.FetchBlocked as e:
        print(f"PARSE FAILED: {e.diagnosis}")
        diagnose(html)
        sys.exit(1)
    return seatmap


def seat_char(seat):
    t = seat.get("type", "")
    avail = seat.get("available", False)
    if t == "Wheelchair":
        return "W" if avail else "w"
    if t == "Companion":
        return "C" if avail else "c"
    return "O" if avail else "X"


def report(seatmap):
    print()
    print("=== Showtime metadata ===")
    for k in ("movie", "theatre", "showDateTimeUtc", "utcOffset"):
        print(f"  {k}: {seatmap[k]}")

    cols = seatmap.get("columns") or 0
    rows = seatmap.get("rows") or 0
    seats = seatmap["seats"]   # NotASeat cells are already filtered out
    print()
    print(f"=== Seating layout: {rows} rows x {cols} columns, {len(seats)} seats ===")

    grid = {(s["row"], s["column"]): s for s in seats}
    print("  legend: . not-a-seat  O available  X occupied  "
          "W/w wheelchair  C/c companion")
    print()
    for r in range(1, rows + 1):
        # row label = first real seat's name letter, if any
        label = " "
        for c in range(1, cols + 1):
            s = grid.get((r, c))
            if s and s.get("name"):
                m = re.match(r"[A-Za-z]+", s["name"])
                label = m.group(0) if m else " "
                break
        line = "".join(seat_char(grid[(r, c)]) if (r, c) in grid else "."
                       for c in range(1, cols + 1))
        print(f"  {label:>2} {line}")

    avail = sorted((s["name"] for s in seats if s["available"]),
                   key=lambda n: (re.match(r"[A-Za-z]+", n).group(0),
                                  int(re.search(r"\d+", n).group(0))))
    print()
    print(f"=== {len(avail)} available of {len(seats)} seats ===")
    print("  " + ", ".join(avail))

    counts = {}
    for s in seats:
        counts[s["type"]] = counts.get(s["type"], 0) + 1
    print()
    print("=== Counts by type ===")
    for t, n in sorted(counts.items(), key=lambda kv: str(kv[0])):
        print(f"  {t}: {n}")

    print()
    print("PROBE OK")


def report_showtimes(theatre, date):
    """Probe the theatre showtimes page — the browse feature's fetch."""
    print(f"Probing showtimes for {theatre} on {date}")
    try:
        listing = amc.fetch_showtimes(theatre, date, log=print)
    except amc.FetchBlocked as e:
        print(f"FETCH FAILED: {e.diagnosis}")
        diagnose(e.body)
        sys.exit(1)
    print()
    print(f"=== {len(listing['movies'])} movies ===")
    for mv in listing["movies"]:
        times = ", ".join(f"{s['time']} ({s['format']})" for s in mv["showings"])
        print(f"  {mv['title']}: {times or 'no showings'}")
    print()
    print("PROBE OK")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    arg = sys.argv[1]
    if os.path.isfile(arg):
        print(f"Parsing local file {arg}")
        with open(arg, encoding="utf-8") as f:
            html = f.read()
    elif "/" in arg:
        theatre, _, date = arg.partition("@")
        report_showtimes(theatre, date or datetime.date.today().isoformat())
        return
    else:
        html = fetch(arg)
    report(parse(html))


if __name__ == "__main__":
    main()
