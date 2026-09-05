"""Unit tests for the parser and matcher. Run: python3 -m unittest discover scripts"""

import email
import gzip
import io
import json
import os
import queue
import types
import unittest
import unittest.mock
import urllib.error
import zlib

import amc
import check
import matcher

FIXTURE = os.path.join(os.path.dirname(__file__), "testdata", "fixture.html")


def load_seatmap():
    with open(FIXTURE, encoding="utf-8") as f:
        return amc.parse_seatmap(f.read(), showtime_id="123")


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.sm = load_seatmap()

    def test_metadata(self):
        self.assertEqual(self.sm["movie"], "Fixture Movie")
        self.assertEqual(self.sm["theatre"], "AMC Fixture 15")
        self.assertEqual(self.sm["showDateTimeUtc"], "2026-06-13T03:15:00.000Z")
        self.assertEqual(self.sm["utcOffset"], "-07:00")
        self.assertEqual(self.sm["showtimeId"], "123")

    def test_grid_dimensions(self):
        self.assertEqual(self.sm["rows"], 4)
        self.assertEqual(self.sm["columns"], 8)

    def test_not_a_seat_filtered(self):
        # 32 cells, 8 are the aisle row
        self.assertEqual(len(self.sm["seats"]), 24)
        self.assertTrue(all(s["type"] != "NotASeat" for s in self.sm["seats"]))

    def test_availability(self):
        avail = {s["name"] for s in self.sm["seats"] if s["available"]}
        self.assertIn("A1", avail)
        self.assertIn("D5", avail)   # column 4
        self.assertIn("D6", avail)   # column 3
        self.assertNotIn("D1", avail)
        self.assertNotIn("C7", avail)  # occupied companion

    def test_seat_name_columns_descend(self):
        a16 = next(s for s in self.sm["seats"] if s["name"] == "A8")
        a1 = next(s for s in self.sm["seats"] if s["name"] == "A1")
        self.assertLess(a16["column"], a1["column"])


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.sm = load_seatmap()

    def test_watched_seats_all_open(self):
        groups = matcher.evaluate(
            {"watchedSeats": ["D5", "D6"], "adjacentRequired": 2}, self.sm)
        self.assertEqual(groups, [["D6", "D5"]])

    def test_watched_seats_partially_taken(self):
        groups = matcher.evaluate(
            {"watchedSeats": ["D1", "D2"], "adjacentRequired": 2}, self.sm)
        self.assertEqual(groups, [])

    def test_gap_breaks_adjacency(self):
        # D5/D6 open but D4/D7 are not: no run of 3 anywhere in row D
        groups = matcher.evaluate(
            {"watchedRows": ["D"], "adjacentRequired": 3}, self.sm)
        self.assertEqual(groups, [])

    def test_watched_rows(self):
        groups = matcher.evaluate(
            {"watchedRows": ["A"], "adjacentRequired": 4}, self.sm)
        self.assertEqual(groups, [["A8", "A7", "A6", "A5", "A4", "A3", "A2", "A1"]])

    def test_exclude_types_breaks_runs(self):
        # row C cols: W c W C  X O O X  -> excluding Wheelchair+Companion
        # leaves only CanReserve C3(col6), C2(col7) available and adjacent
        groups = matcher.evaluate(
            {"watchedRows": ["C"], "adjacentRequired": 2,
             "excludeTypes": ["Wheelchair", "Companion"]}, self.sm)
        self.assertEqual(groups, [["C3", "C2"]])

    def test_exclude_types_removes_match(self):
        groups = matcher.evaluate(
            {"watchedRows": ["C"], "adjacentRequired": 3,
             "excludeTypes": ["Wheelchair", "Companion"]}, self.sm)
        self.assertEqual(groups, [])

    def test_no_filters_whole_room(self):
        groups = matcher.evaluate({"adjacentRequired": 8}, self.sm)
        self.assertEqual(groups, [["A8", "A7", "A6", "A5", "A4", "A3", "A2", "A1"]])

    def test_default_adjacent_is_one(self):
        groups = matcher.evaluate({"watchedSeats": ["D5"]}, self.sm)
        self.assertEqual(groups, [["D5"]])

    def test_signature_stable_and_distinct(self):
        a = matcher.signature([["D6", "D5"]])
        b = matcher.signature([["D5", "D6"]])
        c = matcher.signature([["D5"]])
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class TierTests(unittest.TestCase):
    def test_intervals_are_flat(self):
        # Every watch uses the same interval regardless of time to showtime.
        import check
        day = 24 * 60
        for mins in (5, 30, 8 * 60, day, 7 * day, 30 * day):
            self.assertEqual(check.interval_minutes(mins), check.CHECK_INTERVAL_MIN)


def _showtimes_html(flight):
    """Wrap a raw flight string into a seats-page-style HTML fixture,
    JSON-escaping it the way Next.js emits self.__next_f.push chunks."""
    return "<script>self.__next_f.push([1," + json.dumps(flight) + "])</script>"


# A trimmed but structurally-real theatre showtimes flight: two movies, one
# with two formats, mirroring the id/showtime/movie-link shapes AMC renders.
SHOWTIMES_FLIGHT = (
    '"href":"/movies/moana-72474","target":"_self"}],"children":"Moana"}'
    '"id":"moana-72474-amc-testtown-1-reald3d-0",'
    '"children":[["$","span",null,{"children":"RealD 3D"}]]'
    '{"showtimeId":111,"status":"Sellable",'
    '"showDateTimeUtc":"2026-07-21T16:00:00.000Z",'
    '"display":{"time":"12:00","amPm":"pm"}}'
    '{"showtimeId":112,"status":"Sellable",'
    '"showDateTimeUtc":"2026-07-21T22:30:00.000Z",'
    '"display":{"time":"6:30","amPm":"pm"}}'
    '"id":"moana-72474-amc-testtown-1-standard-1",'
    '"children":[["$","span",null,{"children":"Standard"}]]'
    '{"showtimeId":113,"status":"Sellable",'
    '"showDateTimeUtc":"2026-07-21T18:00:00.000Z",'
    '"display":{"time":"2:00","amPm":"pm"}}'
    '"href":"/movies/the-odyssey-76238","target":"_self"}],'
    '"children":"The Odyssey"}'
    '"id":"the-odyssey-76238-amc-testtown-1-imax-0",'
    '"children":[["$","span",null,{"children":"IMAX"}]]'
    '{"showtimeId":114,"status":"Sellable",'
    '"showDateTimeUtc":"2026-07-21T23:00:00.000Z",'
    '"display":{"time":"7:00","amPm":"pm"}}'
)


class ShowtimesParseTests(unittest.TestCase):
    def setUp(self):
        html = _showtimes_html(SHOWTIMES_FLIGHT)
        self.listing = amc.parse_showtimes(html, "boston/amc-testtown-1")

    def test_theatre_slug_from_path(self):
        self.assertEqual(self.listing["theatreSlug"], "amc-testtown-1")

    def test_movies_and_titles(self):
        titles = [m["title"] for m in self.listing["movies"]]
        self.assertEqual(titles, ["Moana", "The Odyssey"])

    def test_showings_grouped_and_counted(self):
        moana = self.listing["movies"][0]
        self.assertEqual(len(moana["showings"]), 3)
        odyssey = self.listing["movies"][1]
        self.assertEqual(len(odyssey["showings"]), 1)

    def test_showing_fields(self):
        first = self.listing["movies"][0]["showings"][0]
        self.assertEqual(first["showtimeId"], 111)
        self.assertEqual(first["time"], "12:00 pm")
        self.assertEqual(first["format"], "RealD 3D")
        self.assertEqual(first["showDateTimeUtc"], "2026-07-21T16:00:00.000Z")

    def test_format_follows_header(self):
        formats = {s["format"] for s in self.listing["movies"][0]["showings"]}
        self.assertEqual(formats, {"RealD 3D", "Standard"})

    def test_pretty_slug_fallback(self):
        self.assertEqual(amc._pretty_slug("young-washington-80772"),
                         "Young Washington")


class ThrottleClassificationTests(unittest.TestCase):
    def test_throttle_markers_are_not_breakage(self):
        for msg in ["http-429:cloudflare-challenge",
                    "no-seating-layout:cloudflare-challenge",
                    "queue-it-waiting-room", "http-503:unrecognized",
                    "access-denied"]:
            self.assertTrue(check.is_throttle(Exception(msg)), msg)

    def test_genuine_failures_are_breakage(self):
        for msg in ["no-flight-data:unrecognized", "redirect-loop",
                    "no-seating-layout:unrecognized", "KeyError: 'name'"]:
            self.assertFalse(check.is_throttle(Exception(msg)), msg)


class MovieTitleTests(unittest.TestCase):
    def test_flight_placeholder_child_not_captured_as_title(self):
        # A component child renders as ["$","$L3e",...]; the real title lives in
        # the poster alt. The literal "$" must not win (regression: CityWalk
        # showed "The Odyssey" as "$").
        flight = ('"href":"/movies/the-odyssey-76238"}],"children":["$","$L3e",'
                  'null,{"alt":"The Odyssey","height":203}]')
        titles = amc._movie_titles(flight)
        self.assertEqual(titles.get("the-odyssey-76238"), "The Odyssey")

    def test_plain_string_child_title_captured(self):
        flight = ('"href":"/movies/moana-72474","target":"_self"},'
                  '"children":"Moana"}')
        self.assertEqual(amc._movie_titles(flight).get("moana-72474"), "Moana")


class MergeShowtimesTests(unittest.TestCase):
    def _listing(self, showings):
        return {"movies": [{"slug": "moana-72474", "title": "Moana",
                            "showings": showings}]}

    def test_concatenates_across_days_and_sorts(self):
        day1 = self._listing([
            {"showtimeId": 2, "showDateTimeUtc": "2026-07-21T22:30:00Z",
             "time": "6:30 pm", "format": "Standard"},
            {"showtimeId": 1, "showDateTimeUtc": "2026-07-21T16:00:00Z",
             "time": "12:00 pm", "format": "Standard"},
        ])
        day2 = self._listing([
            {"showtimeId": 3, "showDateTimeUtc": "2026-07-22T16:00:00Z",
             "time": "12:00 pm", "format": "Standard"},
        ])
        merged = amc.merge_showtimes([day1, day2])
        self.assertEqual(len(merged), 1)
        ids = [s["showtimeId"] for s in merged[0]["showings"]]
        self.assertEqual(ids, [1, 2, 3])  # chronological across both days

    def test_dedupes_repeated_showtime_ids(self):
        one = self._listing([
            {"showtimeId": 1, "showDateTimeUtc": "2026-07-21T16:00:00Z",
             "time": "12:00 pm", "format": "Standard"},
        ])
        merged = amc.merge_showtimes([one, one])
        self.assertEqual(len(merged[0]["showings"]), 1)

    def test_new_movies_append_in_order(self):
        a = {"movies": [{"slug": "a-1", "title": "A", "showings": []}]}
        b = {"movies": [{"slug": "b-2", "title": "B", "showings": []}]}
        merged = amc.merge_showtimes([a, b])
        self.assertEqual([m["slug"] for m in merged], ["a-1", "b-2"])


class FlightDecodeTests(unittest.TestCase):
    def test_chunks_concatenated(self):
        with open(FIXTURE, encoding="utf-8") as f:
            flight = amc.decode_flight(f.read())
        self.assertIn('"seatingLayout":', flight)

    def test_enclosing_object(self):
        text = '{"a":{"b":"x{y}z","c":1},"d":2}'
        idx = text.find('"c"')
        self.assertEqual(amc.enclosing_object(text, idx), '{"b":"x{y}z","c":1}')


FLIGHT_PAGE = _showtimes_html('"seatingLayout":{"rows":1,"columns":1,"seats":[]}')


class FakeSession:
    """Stands in for amc.Session: replays a scripted list of outcomes.

    Each entry is either an exception to raise or a body to return; every
    ``open`` records the (url, referer) it was asked for so tests can assert
    on session reuse and warm-up.
    """

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.resets = 0
        self._warmed = False

    def reset(self):
        self.resets += 1
        self._warmed = False

    def cookie_names(self):
        return []

    def warm(self, log=lambda m: None):
        self._warmed = True

    def open(self, url, referer=None, timeout=60):
        self.calls.append((url, referer))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome, url, 200


def _blocked(code=403, body="Just a moment"):
    """An HTTPError the way urllib raises one, challenge page and all."""
    return urllib.error.HTTPError(
        "https://www.amctheatres.com/x", code, "Forbidden",
        email.message_from_string("Content-Type: text/html"), io.BytesIO(body.encode()))


class TransientClassificationTests(unittest.TestCase):
    def test_edge_pushback_is_transient(self):
        for msg in ["http-403:cloudflare-challenge", "http-429:unrecognized",
                    "queue-it-waiting-room", "http-503:unrecognized",
                    "access-denied", "http-502:unrecognized"]:
            self.assertTrue(amc.is_transient(Exception(msg)), msg)

    def test_parse_failures_are_not_transient(self):
        # "showtime 403912 vanished" is a broken watcher, not a 403: the
        # markers match the http-NNN: prefix every status arrives with.
        for msg in ["no-flight-data:unrecognized", "redirect-loop",
                    "no-seating-layout:unrecognized", "KeyError: 'name'",
                    "showtime 403912 vanished"]:
            self.assertFalse(amc.is_transient(Exception(msg)), msg)

    def test_poller_reads_the_same_list(self):
        # check.is_throttle is the alerting rule; it must agree with the
        # retry rule or a retried-out block would cry "watcher broken".
        self.assertTrue(check.is_throttle(Exception("http-403:cloudflare-challenge")))
        self.assertFalse(check.is_throttle(Exception("redirect-loop")))


class FetchRetryTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        patch = unittest.mock.patch.object(amc.time, "sleep", self.slept.append)
        patch.start()
        self.addCleanup(patch.stop)

    def test_transient_block_is_retried_from_a_clean_session(self):
        sess = FakeSession([_blocked(), FLIGHT_PAGE])
        html = amc.fetch_page("https://amc/x", session=sess)
        self.assertIn("__next_f", html)
        self.assertEqual(sess.resets, 1)
        self.assertEqual(len(self.slept), 1)

    def test_retries_are_finite_and_then_raise(self):
        sess = FakeSession([_blocked() for _ in range(3)])
        with self.assertRaises(amc.FetchBlocked) as cm:
            amc.fetch_page("https://amc/x", session=sess, retries=2)
        self.assertEqual(cm.exception.diagnosis, "http-403:cloudflare-challenge")
        self.assertEqual(len(sess.calls), 3)   # first attempt + 2 retries

    def test_backoff_grows(self):
        sess = FakeSession([_blocked(), _blocked(), FLIGHT_PAGE])
        amc.fetch_page("https://amc/x", session=sess, retries=2, backoff=5.0)
        self.assertLess(self.slept[0], self.slept[1])

    def test_parse_level_failure_is_not_retried(self):
        # A page with no flight data and no redirect to follow is broken, not
        # throttled: retrying it just wastes a pass.
        sess = FakeSession(["<html>nothing useful here</html>"])
        with self.assertRaises(amc.FetchBlocked):
            amc.fetch_page("https://amc/x", session=sess)
        self.assertEqual(len(sess.calls), 1)

    def test_network_error_is_retried(self):
        sess = FakeSession([urllib.error.URLError("connection reset"), FLIGHT_PAGE])
        self.assertIn("__next_f", amc.fetch_page("https://amc/x", session=sess))


class SessionTests(unittest.TestCase):
    def test_body_decoding(self):
        for encoding, blob in (("gzip", gzip.compress(b"hi")),
                               ("deflate", zlib.compress(b"hi")),
                               ("", b"hi")):
            resp = types.SimpleNamespace(
                read=lambda blob=blob: blob,
                headers={"Content-Encoding": encoding})
            self.assertEqual(amc._read_body(resp), "hi", encoding)

    def test_fetch_site_matches_what_a_browser_would_send(self):
        home = "https://www.amctheatres.com/"
        self.assertEqual(
            amc._fetch_site(home, home + "showtimes/1/seats"), "same-origin")
        self.assertEqual(
            amc._fetch_site(home, "https://queue.amctheatres.com/?c=amc"), "same-site")
        self.assertEqual(amc._fetch_site(home, "https://example.com/"), "cross-site")

    def test_deep_page_arrives_with_a_referer(self):
        # The cold, referer-less hit on a deep URL is what Cloudflare
        # challenges; every page must come in behind the warmed homepage.
        sess = FakeSession([FLIGHT_PAGE])
        amc.fetch_page("https://www.amctheatres.com/showtimes/1/seats", session=sess)
        self.assertTrue(sess._warmed)
        self.assertEqual(sess.calls[0][1], amc.HOME_URL)

    def test_reset_clears_cookies(self):
        sess = amc.Session()
        jar = sess.jar
        sess._warmed = True
        sess.reset()
        self.assertIsNot(sess.jar, jar)
        self.assertFalse(sess._warmed)


class ShowtimesRangeTests(unittest.TestCase):
    """A multi-day browse shares one session and survives a blocked day."""

    def setUp(self):
        patch = unittest.mock.patch.object(amc.time, "sleep", lambda s: None)
        patch.start()
        self.addCleanup(patch.stop)

    def _run(self, blocked_days, days=3):
        self.sessions = []

        def fake_fetch(theatre, date, log, session=None):
            self.sessions.append(session)
            if date in blocked_days:
                raise amc.FetchBlocked("http-403:cloudflare-challenge")
            return {"movies": [{"slug": "m-1", "title": "M", "showings": [
                {"showtimeId": int(date.replace("-", "")),
                 "showDateTimeUtc": date + "T16:00:00Z",
                 "time": "12:00 pm", "format": "Standard"}]}]}

        with unittest.mock.patch.object(amc, "fetch_showtimes", fake_fetch):
            return amc.fetch_showtimes_range("boston/amc-x-1", "2026-07-21", days)

    def test_blocked_day_does_not_lose_the_others(self):
        listing = self._run({"2026-07-22"})
        self.assertEqual(listing["failedDates"], ["2026-07-22"])
        self.assertEqual(len(listing["movies"][0]["showings"]), 2)

    def test_clean_range_reports_no_failures(self):
        self.assertEqual(self._run(set())["failedDates"], [])

    def test_one_session_serves_the_whole_range(self):
        self._run(set())
        self.assertEqual(len(set(map(id, self.sessions))), 1)

    def test_every_day_blocked_still_raises(self):
        with self.assertRaises(amc.FetchBlocked):
            self._run({"2026-07-21", "2026-07-22", "2026-07-23"})


class SessionPoolTests(unittest.TestCase):
    """The poller reuses warm sessions rather than paying the homepage hop —
    and looking like a brand-new visitor — on every 15-second check."""

    def setUp(self):
        self.seen = []

        def fake_fetch(sid, log=None, session=None):
            self.seen.append(session)
            if sid == "boom":
                raise RuntimeError("fetch exploded")
            return {"showtimeId": sid}

        patch = unittest.mock.patch.object(amc, "fetch_seatmap", fake_fetch)
        patch.start()
        self.addCleanup(patch.stop)
        self._drain()
        self.addCleanup(self._drain)

    @staticmethod
    def _drain():
        while True:
            try:
                check._SESSIONS.get_nowait()
            except queue.Empty:
                return

    def test_session_is_reused_across_fetches(self):
        check.fetch_watch("1")
        check.fetch_watch("2")
        self.assertIsNotNone(self.seen[0])
        self.assertIs(self.seen[0], self.seen[1])

    def test_session_is_returned_even_when_the_fetch_fails(self):
        self.assertIsInstance(check.fetch_watch("boom"), RuntimeError)
        check.fetch_watch("1")
        self.assertIs(self.seen[0], self.seen[1])


if __name__ == "__main__":
    unittest.main()
