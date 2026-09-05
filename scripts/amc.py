"""Fetch and parse AMC seat maps.

The seat data is server-rendered into the page as Next.js flight chunks
(self.__next_f.push). Reaching the page anonymously takes a short
redirect dance through Queue-It's "Global Safety Net" waiting room; with
no active queue it waves the request straight through. All plain HTTP,
stdlib only.

Cloudflare sits in front of all of it and challenges (HTTP 403 "Just a
moment", or 429) requests that look automated. Two things keep that rare,
and one keeps it survivable:

  * a Session arrives via the site root and reuses the cookies Cloudflare
    and Queue-It set, instead of hitting a deep URL cold once per page;
  * the request headers are a real browser's, gzip and Referer included;
  * a block is retried from a clean session with jittered backoff, because
    it is almost always transient — especially from a datacenter IP like a
    GitHub Actions runner, where the whole range is shared and warm.
"""

import datetime
import gzip
import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

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
    # urllib's default is "identity", which no browser sends and every bot
    # filter notices; _read_body undoes whichever of these we get back.
    "Accept-Encoding": "gzip, deflate",
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
HOME_URL = "https://www.amctheatres.com/"

# A blocked fetch is retried this many times from a clean session, waiting
# ~5s, ~10s, then ~20s (plus jitter, and capped so a long tail stays bounded).
# Measured from a GitHub runner in Sep 2026, roughly two cold attempts in
# three come back 403 and the next one sails through, so a budget of 2 landed
# on the last allowed try — hence 4. The poller passes its own smaller budget
# (check.POLL_RETRIES): it re-checks every ~15s anyway, and its sessions stay
# warm between passes.
RETRIES = 4
RETRY_BACKOFF = 5.0
RETRY_MAX_DELAY = 20.0

# Jittered gap between the day-pages of a multi-day browse: a human clicking
# through dates doesn't fire them back to back, and neither should we.
DAY_SLEEP_RANGE = (1.5, 4.0)


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


# Pushback from AMC's edge is transient far more often than not: the same URL
# usually goes through seconds later from a clean session. These markers match
# both the diagnoses above and the HTTP statuses the edge throttles with, and
# are the one list both the fetch retry and the poller's "don't cry broken"
# rule (check.is_throttle) read from.
TRANSIENT_MARKERS = ("http-403", "http-429", "http-502", "http-503",
                     "cloudflare-challenge", "queue-it", "access-denied")


def is_transient(exc):
    """True if ``exc`` is AMC saying "slow down" rather than "you're broken"."""
    msg = str(exc).lower()
    return any(m in msg for m in TRANSIENT_MARKERS)


def _read_body(resp):
    """Read a response (or HTTPError) body, undoing the encoding we asked for."""
    raw = resp.read()
    encoding = (resp.headers.get("Content-Encoding") or "").lower()
    if "gzip" in encoding:
        raw = gzip.decompress(raw)
    elif "deflate" in encoding:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:  # raw deflate, no zlib header
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def _origin(url):
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _fetch_site(from_url, to_url):
    """The Sec-Fetch-Site value a browser would send for this navigation."""
    src = urllib.parse.urlsplit(from_url).netloc
    dst = urllib.parse.urlsplit(to_url).netloc
    if src == dst:
        return "same-origin"
    # registrable domain, close enough for amctheatres.com vs queue.amctheatres.com
    return "same-site" if (src.split(".")[-2:] == dst.split(".")[-2:]) else "cross-site"


class Session:
    """One browsing session: a cookie jar and the opener that fills it.

    Cloudflare challenges a cold, cookie-less request for a deep URL far more
    readily than one from a session that arrived via the site root, so a
    session lands on the homepage once (``warm``) and then reuses the cookies
    Cloudflare and Queue-It set for every page after it. Reuse one across a
    run: N pages through one warm session look far less robotic than N cold
    ones, and cost one fewer request each.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Throw the session away and start clean — what to do after a block."""
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self._warmed = False

    def cookie_names(self):
        """Cookie names only — these logs are public, values never are."""
        return sorted({c.name for c in self.jar})

    def open(self, url, referer=None, timeout=60):
        """GET ``url`` through the jar; returns (body, final_url, status)."""
        headers = dict(HEADERS)
        if referer:
            site = _fetch_site(referer, url)
            headers["Sec-Fetch-Site"] = site
            # Chrome's default strict-origin-when-cross-origin policy: the full
            # URL to the same origin, a bare origin to anywhere else.
            headers["Referer"] = referer if site == "same-origin" else _origin(referer)
        req = urllib.request.Request(url, headers=headers)
        with self.opener.open(req, timeout=timeout) as resp:
            return _read_body(resp), resp.geturl(), resp.status

    def warm(self, log=lambda msg: None):
        """Land on the site root once, for the cookies a deep page expects.

        Best-effort: if the homepage itself is blocked there is nothing to be
        gained by giving up here, so we note it and go on to the real page.
        """
        if self._warmed:
            return
        self._warmed = True  # one attempt per session, whether or not it worked
        try:
            _, url, status = self.open(HOME_URL)
            log(f"[warm] HTTP {status} {url}, "
                f"cookies: {', '.join(self.cookie_names()) or 'none'}")
        except OSError as e:  # URLError/HTTPError/timeouts are all OSError
            log(f"[warm] homepage fetch failed ({e}); continuing without it")


def fetch_page(url, log=lambda msg: None, session=None,
               retries=RETRIES, backoff=RETRY_BACKOFF):
    """Fetch an AMC page's HTML, following the Queue-It redirect chain.

    Returns the first response body that carries Next.js flight data
    (``__next_f``). A transient block (Cloudflare 403/429, the Queue-It
    waiting room, a network blip) is retried from a clean session with
    jittered backoff; FetchBlocked is raised once the retries are spent, and
    straight away for anything a retry can't fix (a parse-level failure, a
    redirect loop). Pass a ``session`` to share cookies across pages — it is
    reset before each retry, since a blocked session stays blocked.
    """
    sess = session or Session()
    for attempt in range(retries + 1):
        try:
            return _fetch_page_once(url, sess, log)
        except (FetchBlocked, OSError) as e:  # URLError/timeouts are OSError
            retryable = is_transient(e) if isinstance(e, FetchBlocked) else True
            if attempt == retries or not retryable:
                raise
            delay = min(backoff * 2 ** attempt, RETRY_MAX_DELAY) + random.uniform(0, 2)
            log(f"blocked ({e}); retrying in {delay:.0f}s from a clean session "
                f"[{attempt + 1}/{retries}]")
            sess.reset()
            time.sleep(delay)


def _fetch_page_once(url, sess, log):
    """One attempt at fetch_page: warm the session, then follow the hops."""
    sess.warm(log)
    referer = HOME_URL

    for hop in range(1, MAX_HOPS + 1):
        try:
            body, final_url, status = sess.open(url, referer=referer)
        except urllib.error.HTTPError as e:
            body = _read_body(e)
            raise FetchBlocked(f"http-{e.code}:{diagnose(body)}", body)

        log(f"[hop {hop}] HTTP {status}, {len(body)} bytes, landed on {final_url}, "
            f"cookies: {', '.join(sess.cookie_names()) or 'none'}")

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
        referer = final_url
        url = urllib.parse.urljoin(final_url, target)

    raise FetchBlocked("redirect-loop")


def fetch_html(showtime_id, log=lambda msg: None, session=None, retries=RETRIES):
    """Fetch the seats page HTML, following the Queue-It redirect chain."""
    return fetch_page(
        f"https://www.amctheatres.com/showtimes/{showtime_id}/seats", log, session,
        retries=retries)


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


def fetch_seatmap(showtime_id, log=lambda msg: None, session=None, retries=RETRIES):
    return parse_seatmap(
        fetch_html(showtime_id, log, session, retries), showtime_id)


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
    """Map movie slug -> human title, from movie links and poster alts.

    The ``(?!\\$)`` guard skips Next.js flight placeholders: a component child
    rendered as ``"children":["$","$L3e",...]`` would otherwise be captured as
    the literal ``$``, clobbering the real title (e.g. "The Odyssey").
    """
    titles = {}
    for m in re.finditer(
            r'/movies/([a-z0-9-]+)"[^\]]{0,160}?"children":(?:"|\[")'
            r'(?!\$)([^"]{1,90})"', flight, re.S):
        titles.setdefault(m.group(1), m.group(2))
    for m in re.finditer(
            r'/movies/([a-z0-9-]+)"(?:.{0,500}?)"alt":"(?!\$)([^"]{1,90})"',
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


def fetch_showtimes(theatre, date, log=lambda msg: None, session=None):
    """Fetch and parse a theatre's showtimes for a date (YYYY-MM-DD).

    ``theatre`` is the path after /movie-theatres/, e.g.
    'boston/amc-boston-common-19' (market/slug); a bare slug also works.
    """
    path = theatre.strip().strip("/").split("?")[0]
    url = (f"https://www.amctheatres.com/movie-theatres/{path}/showtimes"
           f"?date={date}")
    result = parse_showtimes(fetch_page(url, log, session), path)
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

    One AMC request per day, sequential and gently spaced, all through a
    single warm session. A day that stays blocked after its retries is
    recorded in ``failedDates`` rather than throwing away the days that did
    come back; FetchBlocked is raised only if every day was blocked.

    Returns the same shape as fetch_showtimes plus ``days`` and
    ``failedDates``, with each movie's showings spanning the whole range.
    """
    days = max(1, int(days))
    d0 = datetime.date.fromisoformat(start_date)
    path = theatre.strip().strip("/").split("?")[0]
    session = Session()
    listings = []
    failed = []
    blocked = None
    for i in range(days):
        day = (d0 + datetime.timedelta(days=i)).isoformat()
        if i:
            time.sleep(random.uniform(*DAY_SLEEP_RANGE))  # don't burst the range
        log(f"fetching {day} ({i + 1}/{days})")
        try:
            listings.append(fetch_showtimes(path, day, log, session))
        except FetchBlocked as e:
            log(f"{day}: blocked ({e.diagnosis}); skipping this day")
            failed.append(day)
            blocked = e
            session.reset()
    if not listings:
        raise blocked
    return {
        "theatre": path,
        "theatreSlug": _theatre_slug(path),
        "date": start_date,
        "days": days,
        "failedDates": failed,
        "movies": merge_showtimes(listings),
        "fetchedAtUtc": (datetime.datetime.now(datetime.timezone.utc)
                         .strftime("%Y-%m-%dT%H:%M:%SZ")),
    }
