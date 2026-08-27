#!/usr/bin/env python3
"""Phase 0 throwaway probe: fetch one AMC seat map and print parsed seats.

Usage:
    python3 probe.py <showtimeId>        # fetch live page, parse, print
    python3 probe.py <path-to-html>      # parse a saved HTML file (offline test)

Stdlib only. Starts anonymous (empty cookie jar) and follows the
cookie-test / Queue-It redirect chain in plain HTTP, emulating the one
JS redirect the cookie-test page performs. Logs every hop so we can see
exactly which protection layer stops us, if any.
"""

import http.cookiejar
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

FLIGHT_RE = re.compile(r'self\.__next_f\.push\(\[1,\s*"((?:\\.|[^"\\])*)"\]\)')

# JS redirects we know how to emulate without a browser
COOKIE_TEST_RE = re.compile(r"document\.location\.href\s*=\s*decodeURIComponent\('([^']+)'\)")
META_REFRESH_RE = re.compile(
    r'http-equiv=["\']refresh["\'][^>]*?url\s*=\s*([^"\'>\s]+)', re.I)
WINDOW_LOC_RE = re.compile(r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""")

MAX_HOPS = 8


def fetch(showtime_id, url=None):
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    if url is None:
        url = f"https://www.amctheatres.com/showtimes/{showtime_id}/seats"

    for hop in range(1, MAX_HOPS + 1):
        print(f"[hop {hop}] GET {url}")
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with opener.open(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                final_url = resp.geturl()
                status = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"[hop {hop}] HTTP {e.code}, {len(body)} bytes")
            diagnose(body)
            sys.exit(1)
        except Exception as e:
            print(f"[hop {hop}] FETCH FAILED: {type(e).__name__}: {e}")
            sys.exit(1)

        # never print cookie values: this log is public
        names = sorted({c.name for c in jar})
        print(f"[hop {hop}] HTTP {status}, {len(body)} bytes, "
              f"landed on {final_url}, cookies: {names or 'none'}")

        if "__next_f" in body:
            print(f"[hop {hop}] flight data present — done after {hop} hop(s)")
            return body

        target = None
        m = COOKIE_TEST_RE.search(body)
        if m:
            target = urllib.parse.unquote(m.group(1))
            print(f"[hop {hop}] cookie-test page; emulating its JS redirect")
        if target is None:
            m = META_REFRESH_RE.search(body)
            if m:
                target = m.group(1)
                print(f"[hop {hop}] meta-refresh redirect")
        if target is None:
            m = WINDOW_LOC_RE.search(body)
            if m:
                target = m.group(1)
                print(f"[hop {hop}] window.location JS redirect")
        if target is None:
            print(f"[hop {hop}] no flight data and no followable redirect")
            diagnose(body)
            print("---- FULL BODY (debug) ----")
            print(body)
            print("---- END BODY ----")
            sys.exit(1)

        url = urllib.parse.urljoin(final_url, target)

    print(f"Gave up after {MAX_HOPS} hops (redirect loop?)")
    sys.exit(1)


def diagnose(body):
    """Print which bot-protection layer we hit, if recognizable."""
    if "Just a moment" in body or "cf-chl" in body or "challenge-platform" in body:
        print("DIAGNOSIS: Cloudflare challenge page (bot check).")
    elif "queue.amctheatres.com" in body or "queue-it" in body.lower():
        print("DIAGNOSIS: Queue-It virtual waiting room redirect.")
    elif "Access denied" in body or "blocked" in body.lower():
        print("DIAGNOSIS: Access denied / blocked.")
    else:
        print("DIAGNOSIS: unrecognized failure. First 500 chars of body:")
        print(body[:500])


def decode_flight(html):
    """Concatenate all self.__next_f.push([1,"..."]) string chunks, unescaped."""
    parts = []
    for m in FLIGHT_RE.finditer(html):
        parts.append(json.loads('"' + m.group(1) + '"'))
    return "".join(parts)


def extract_object(text, start):
    """Extract a balanced {...} JSON object starting at text[start] == '{'.

    String- and escape-aware so braces inside string values don't break it.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    raise ValueError("unbalanced braces")


def find_value(flight, key):
    """Pull a simple string value like "key":"value" out of the flight text."""
    m = re.search(r'"%s"\s*:\s*"([^"]*)"' % re.escape(key), flight)
    return m.group(1) if m else None


def parse(html):
    flight = decode_flight(html)
    if not flight:
        print("PARSE FAILED: no __next_f flight chunks found in document.")
        diagnose(html)
        sys.exit(1)

    idx = flight.find('"seatingLayout":')
    if idx == -1:
        print("PARSE FAILED: flight data present but no seatingLayout key.")
        diagnose(html)
        sys.exit(1)

    brace = flight.index("{", idx)
    layout = json.loads(extract_object(flight, brace))

    meta = {
        "movie": find_value(flight, "movieName") or find_value(flight, "name"),
        "theatre": find_value(flight, "longName") or find_value(flight, "theatreName"),
        "showDateTimeUtc": find_value(flight, "showDateTimeUtc"),
        "utcOffset": find_value(flight, "utcOffset"),
    }
    return layout, meta


SYMBOLS = {
    # (type-ish, available) -> char; resolved in seat_char()
}


def seat_char(seat):
    t = seat.get("type", "")
    avail = seat.get("available", False)
    if t == "NotASeat" or not seat.get("shouldDisplay", True):
        return "."
    if t == "Wheelchair":
        return "W" if avail else "w"
    if t == "Companion":
        return "C" if avail else "c"
    return "O" if avail else "X"


def report(layout, meta):
    print()
    print("=== Showtime metadata ===")
    for k, v in meta.items():
        print(f"  {k}: {v}")

    cols = layout.get("columns")
    rows = layout.get("rows")
    seats = layout.get("seats", [])
    print()
    print(f"=== Seating layout: {rows} rows x {cols} columns, {len(seats)} cells ===")

    grid = {}
    for s in seats:
        grid[(s["row"], s["column"])] = s

    legend = ". not-a-seat  O available  X occupied  W/w wheelchair  C/c companion"
    print(f"  legend: {legend}")
    print()
    for r in range(1, (rows or 0) + 1):
        # row label = first real seat's name letter, if any
        label = " "
        for c in range(1, (cols or 0) + 1):
            s = grid.get((r, c))
            if s and s.get("name"):
                label = re.match(r"[A-Za-z]+", s["name"])
                label = label.group(0) if label else " "
                break
        line = "".join(seat_char(grid[(r, c)]) if (r, c) in grid else " "
                       for c in range(1, (cols or 0) + 1))
        print(f"  {label:>2} {line}")

    real = [s for s in seats if s.get("type") != "NotASeat" and s.get("shouldDisplay", True)]
    avail = sorted((s["name"] for s in real if s.get("available")),
                   key=lambda n: (re.match(r"[A-Za-z]+", n).group(0),
                                  int(re.search(r"\d+", n).group(0))))
    print()
    print(f"=== {len(avail)} available of {len(real)} seats ===")
    print("  " + ", ".join(avail))

    counts = {}
    for s in real:
        counts[s.get("type")] = counts.get(s.get("type"), 0) + 1
    print()
    print("=== Counts by type ===")
    for t, n in sorted(counts.items()):
        print(f"  {t}: {n}")

    print()
    print("PROBE OK")


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    arg = sys.argv[1]
    if arg.startswith("browse:"):
        # debug: browse:<theatre-path>:<YYYY-MM-DD>, e.g.
        # browse:los-angeles/amc-century-city-15:2026-08-28
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import amc
        _, theatre, date = arg.split(":", 2)
        try:
            result = amc.fetch_showtimes(theatre, date, log=print)
            print(f"OK: {len(result['movies'])} movies, "
                  f"{sum(len(m['showings']) for m in result['movies'])} showings")
        except amc.FetchBlocked as e:
            print(f"FetchBlocked: {e.diagnosis}")
            print("---- FULL BODY (debug) ----")
            print(e.body)
            print("---- END BODY ----")
        return
    if os.path.isfile(arg):
        print(f"Parsing local file {arg}")
        with open(arg, encoding="utf-8") as f:
            html = f.read()
    else:
        html = fetch(arg)
    layout, meta = parse(html)
    report(layout, meta)


if __name__ == "__main__":
    main()
