"""Fetch and parse AMC seat maps.

The seat data is server-rendered into the page as Next.js flight chunks
(self.__next_f.push). Reaching the page anonymously takes a short
redirect dance through Queue-It's "Global Safety Net" waiting room; with
no active queue it waves the request straight through. All plain HTTP,
stdlib only.
"""

import datetime
import http.cookiejar
import json
import re
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
COOKIE_TEST_RE = re.compile(
    r"document\.location\.href\s*=\s*decodeURIComponent\('([^']+)'\)")
META_REFRESH_RE = re.compile(
    r'http-equiv=["\']refresh["\'][^>]*?url\s*=\s*([^"\'>\s]+)', re.I)
WINDOW_LOC_RE = re.compile(r"""window\.location(?:\.href)?\s*=\s*["']([^"']+)["']""")

MAX_HOPS = 8


class FetchBlocked(Exception):
    """The protection stack stopped us; .diagnosis says which layer."""

    def __init__(self, diagnosis, body=""):
        super().__init__(diagnosis)
        self.diagnosis = diagnosis
        self.body = body


def diagnose(body):
    if "Just a moment" in body or "cf-chl" in body or "challenge-platform" in body:
        return "cloudflare-challenge"
    if "queue.amctheatres.com" in body or "queue-it" in body.lower():
        return "queue-it-waiting-room"
    if "Access denied" in body or "blocked" in body.lower():
        return "access-denied"
    return "unrecognized"


def fetch_html(showtime_id, log=lambda msg: None):
    """Fetch the seats page HTML, following the Queue-It redirect chain."""
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url = f"https://www.amctheatres.com/showtimes/{showtime_id}/seats"

    for hop in range(1, MAX_HOPS + 1):
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            with opener.open(req, timeout=60) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                final_url = resp.geturl()
                status = resp.status
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise FetchBlocked(f"http-{e.code}:{diagnose(body)}", body)

        log(f"[hop {hop}] HTTP {status}, {len(body)} bytes, landed on {final_url}")

        if "__next_f" in body:
            return body

        target = None
        for pattern, decode in ((COOKIE_TEST_RE, True), (META_REFRESH_RE, False),
                                (WINDOW_LOC_RE, False)):
            m = pattern.search(body)
            if m:
                target = urllib.parse.unquote(m.group(1)) if decode else m.group(1)
                break
        if target is None:
            raise FetchBlocked(diagnose(body), body)
        url = urllib.parse.urljoin(final_url, target)

    raise FetchBlocked("redirect-loop")


def decode_flight(html):
    """Concatenate all flight chunks into one unescaped string."""
    parts = []
    for m in FLIGHT_RE.finditer(html):
        parts.append(json.loads('"' + m.group(1) + '"'))
    return "".join(parts)


def extract_object(text, start):
    """Extract a balanced {...} starting at text[start] == '{'.

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
                    return text[start:i + 1]
    raise ValueError("unbalanced braces")


def enclosing_object(text, idx):
    """Extract the innermost {...} object that contains position idx."""
    stack = []
    in_str = False
    esc = False
    enclosing_start = None
    for i, c in enumerate(text):
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
                stack.append(i)
            elif c == "}":
                start = stack.pop() if stack else None
                if enclosing_start is not None and start == enclosing_start:
                    return text[start:i + 1]
        if i == idx:
            if not stack:
                raise ValueError("position not inside an object")
            enclosing_start = stack[-1]
    raise ValueError("unbalanced braces")


def _meta_string(obj, *keys):
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v:
            return v
    return None


def parse_seatmap(html, showtime_id=None):
    """Parse the seats page into a normalized seatmap dict."""
    flight = decode_flight(html)
    if not flight:
        raise FetchBlocked("no-flight-data:" + diagnose(html), html)

    idx = flight.find('"seatingLayout":')
    if idx == -1:
        raise FetchBlocked("no-seating-layout:" + diagnose(html), html)
    layout = json.loads(extract_object(flight, flight.index("{", idx)))

    meta = {"movie": None, "theatre": None,
            "showDateTimeUtc": None, "utcOffset": None}
    sdt = flight.find('"showDateTimeUtc"')
    if sdt != -1:
        try:
            obj = json.loads(enclosing_object(flight, sdt))
            meta["showDateTimeUtc"] = obj.get("showDateTimeUtc")
            meta["movie"] = _meta_string(obj, "movieName", "movieTitle", "title")
            theatre = obj.get("theatre")
            if isinstance(theatre, dict):
                meta["theatre"] = _meta_string(theatre, "longName", "name")
                meta["utcOffset"] = theatre.get("utcOffset") or meta["utcOffset"]
        except ValueError:
            pass
        m = re.search(r'"showDateTimeUtc"\s*:\s*"([^"]*)"', flight)
        if meta["showDateTimeUtc"] is None and m:
            meta["showDateTimeUtc"] = m.group(1)
    if meta["utcOffset"] is None:
        m = re.search(r'"utcOffset"\s*:\s*"([^"]*)"', flight)
        meta["utcOffset"] = m.group(1) if m else None

    seats = []
    for s in layout.get("seats", []):
        if s.get("type") == "NotASeat" or not s.get("shouldDisplay", True):
            continue
        seats.append({
            "name": s["name"],
            "row": s["row"],
            "column": s["column"],
            "type": s.get("type"),
            "available": bool(s.get("available")),
        })

    return {
        "showtimeId": str(showtime_id) if showtime_id else None,
        "fetchedAtUtc": datetime.datetime.now(datetime.timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "movie": meta["movie"],
        "theatre": meta["theatre"],
        "showDateTimeUtc": meta["showDateTimeUtc"],
        "utcOffset": meta["utcOffset"],
        "rows": layout.get("rows"),
        "columns": layout.get("columns"),
        "seats": seats,
    }


def fetch_seatmap(showtime_id, log=lambda msg: None):
    return parse_seatmap(fetch_html(showtime_id, log), showtime_id)
