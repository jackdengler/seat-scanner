"""Unit tests for the parser and matcher. Run: python3 -m unittest discover scripts"""

import json
import os
import unittest

import amc
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


if __name__ == "__main__":
    unittest.main()
