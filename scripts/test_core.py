"""Unit tests for the parser and matcher. Run: python3 -m unittest discover scripts"""

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
    def test_intervals(self):
        import check
        day = 24 * 60
        self.assertEqual(check.interval_minutes(30), 0)
        self.assertEqual(check.interval_minutes(4 * 60), 0)
        self.assertEqual(check.interval_minutes(5 * 60), 15)
        self.assertEqual(check.interval_minutes(day), 15)
        self.assertEqual(check.interval_minutes(2 * day), 30)
        self.assertEqual(check.interval_minutes(7 * day), 30)
        self.assertEqual(check.interval_minutes(8 * day), 360)


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
