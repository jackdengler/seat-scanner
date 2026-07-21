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


def fetch_page(url, log=lambda msg: None):
    """Fetch an AMC page's HTML, following the Queue-It redirect chain.

    Returns the first response body that carries Next.js flight data
    (``__next_f``); raises FetchBlocked if the protection stack stops us.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

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


def fetch_html(showtime_id, log=lambda msg: None):
    """Fetch the seats page HTML, following the Queue-It redirect chain."""
    return fetch_page(
        f"https://www.amctheatres.com/showtimes/{showtime_id}/seats", log)


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


# ---- showtimes listing (browse a theatre by date) ----
#
# The theatre showtimes page renders each showing as a React server-component
# tree rather than a clean JSON blob, so parsing keys off three stable shapes:
#   * a format-group heading  "id":"<movie-slug>-<theatre-slug>-<format>-<n>"
#   * a showtime object       {"showtimeId":N,...,"showDateTimeUtc":"...",
#                              "display":{"time":"12:00","amPm":"pm"}}
#   * a movie link+title       href":"/movies/<slug>" ... "children":"<Title>"
# Showings are grouped under whichever heading most recently preceded them.

_THEATRE_SLUG_RE = re.compile(r"[a-z0-9-]+$")
_SHOWTIME_RE = re.compile(
    r'\{"showtimeId":(\d+),.*?"showDateTimeUtc":"([^"]+)"'
    r'.*?"time":"([^"]+)","amPm":"([^"]+)"\}')


def _theatre_slug(theatre):
    """Last path segment of a theatre path/URL, e.g. 'amc-boston-common-19'."""
    m = _THEATRE_SLUG_RE.search(theatre.strip().strip("/").split("?")[0])
    return m.group(0) if m else theatre.strip("/")


def _movie_titles(flight):
    """Map movie slug -> human title, from movie links and poster alts."""
    titles = {}
    for m in re.finditer(
            r'/movies/([a-z0-9-]+)"[^\]]{0,160}?"children":(?:"|\[")'
            r'([^"]{1,90})"', flight, re.S):
        titles.setdefault(m.group(1), m.group(2))
    for m in re.finditer(
            r'/movies/([a-z0-9-]+)"(?:.{0,500}?)"alt":"([^"]{1,90})"',
            flight, re.S):
        titles.setdefault(m.group(1), m.group(2))
    return titles


def _pretty_slug(slug):
    """Fallback title when a movie link/alt wasn't found: de-slug the id."""
    return re.sub(r"-\d+$", "", slug).replace("-", " ").title() or slug


def parse_showtimes(html, theatre_slug):
    """Parse a theatre showtimes page into a list of movies with showings.

    Returns {"theatreSlug", "movies": [{"slug","title","showings":[
      {"showtimeId","showDateTimeUtc","time","format"}]}]}, movies and
    showings in page order.
    """
    flight = decode_flight(html)
    if not flight:
        raise FetchBlocked("no-flight-data:" + diagnose(html), html)

    slug = _theatre_slug(theatre_slug)
    titles = _movie_titles(flight)
    header_re = re.compile(
        r'"id":"([a-z0-9-]+?-\d+)-' + re.escape(slug) + r'-([a-z0-9]+)-\d+"')

    # Collect group headers and showtimes as (position, kind, data), ordered.
    tokens = []
    for m in header_re.finditer(flight):
        # First visible label after the id is the format name ("RealD 3D").
        fmt = re.search(r'"children":"([^"]{1,40})"', flight[m.end():m.end() + 400])
        tokens.append((m.start(), "hdr",
                       (m.group(1), fmt.group(1) if fmt else m.group(2))))
    for m in _SHOWTIME_RE.finditer(flight):
        tokens.append((m.start(), "show", m.groups()))
    tokens.sort(key=lambda t: t[0])

    movies = {}   # slug -> movie dict (insertion order preserved)
    order = []
    current = None
    for _, kind, data in tokens:
        if kind == "hdr":
            movie_slug, current_fmt = data
            current = movie_slug
            if movie_slug not in movies:
                movies[movie_slug] = {
                    "slug": movie_slug,
                    "title": titles.get(movie_slug) or _pretty_slug(movie_slug),
                    "showings": [],
                }
                order.append(movie_slug)
        elif current is not None:
            sid, utc, tm, ampm = data
            movies[current]["showings"].append({
                "showtimeId": int(sid),
                "showDateTimeUtc": utc,
                "time": f"{tm} {ampm}",
                "format": current_fmt,
            })

    return {"theatreSlug": slug, "movies": [movies[s] for s in order]}


def fetch_showtimes(theatre, date, log=lambda msg: None):
    """Fetch and parse a theatre's showtimes for a date (YYYY-MM-DD).

    ``theatre`` is the path after /movie-theatres/, e.g.
    'boston/amc-boston-common-19' (market/slug); a bare slug also works.
    """
    path = theatre.strip().strip("/").split("?")[0]
    url = (f"https://www.amctheatres.com/movie-theatres/{path}/showtimes"
           f"?date={date}")
    result = parse_showtimes(fetch_page(url, log), path)
    result["date"] = date
    result["theatre"] = path
    result["fetchedAtUtc"] = (datetime.datetime.now(datetime.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"))
    return result


def merge_showtimes(listings):
    """Merge several single-day listings into one movie->showings list.

    Movies are keyed by slug and keep first-seen order; each movie's showings
    are concatenated, de-duplicated by showtimeId, and sorted chronologically.
    """
    movies = {}
    order = []
    for listing in listings:
        for mv in listing.get("movies", []):
            m = movies.get(mv["slug"])
            if m is None:
                m = {"slug": mv["slug"], "title": mv["title"], "showings": []}
                movies[mv["slug"]] = m
                order.append(mv["slug"])
            m["showings"].extend(mv["showings"])
    for m in movies.values():
        seen = set()
        uniq = []
        for s in sorted(m["showings"], key=lambda s: s["showDateTimeUtc"]):
            if s["showtimeId"] in seen:
                continue
            seen.add(s["showtimeId"])
            uniq.append(s)
        m["showings"] = uniq
    return [movies[s] for s in order]


def fetch_showtimes_range(theatre, start_date, days, log=lambda msg: None):
    """Fetch ``days`` consecutive days starting at ``start_date`` and merge.

    One AMC request per day (kept sequential and gentle). Returns the same
    shape as fetch_showtimes plus a ``days`` field, with each movie's showings
    spanning the whole range.
    """
    days = max(1, int(days))
    d0 = datetime.date.fromisoformat(start_date)
    path = theatre.strip().strip("/").split("?")[0]
    listings = []
    for i in range(days):
        day = (d0 + datetime.timedelta(days=i)).isoformat()
        log(f"fetching {day} ({i + 1}/{days})")
        listings.append(fetch_showtimes(path, day, log))
    return {
        "theatre": path,
        "theatreSlug": _theatre_slug(path),
        "date": start_date,
        "days": days,
        "movies": merge_showtimes(listings),
        "fetchedAtUtc": (datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ")),
    }
