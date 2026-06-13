"""Decide which seat groups satisfy a watch's rules.

Watch fields used (all optional):
  watchedSeats     explicit seat names, e.g. ["E5", "E6"]; wins over rows
  watchedRows      row letters, e.g. ["D", "E"]
  adjacentRequired minimum run of physically adjacent available seats (default 1)
  excludeTypes     seat types ignored entirely, e.g. ["Wheelchair", "Companion"]

Adjacency means same row and consecutive columns — the grid columns are
authoritative, and aisles/excluded seats break a run.
"""

import hashlib
import json
import re


def _row_letter(name):
    m = re.match(r"[A-Za-z]+", name or "")
    return m.group(0).upper() if m else ""


def evaluate(watch, seatmap):
    """Return the list of qualifying seat groups (each a list of names)."""
    exclude = set(watch.get("excludeTypes") or [])
    seats = [s for s in seatmap["seats"] if s["type"] not in exclude]

    watched_seats = watch.get("watchedSeats") or []
    watched_rows = {r.upper() for r in (watch.get("watchedRows") or [])}
    need = max(1, int(watch.get("adjacentRequired") or 1))

    if watched_seats:
        wanted = set(watched_seats)
        pool = [s for s in seats if s["name"] in wanted]
    elif watched_rows:
        pool = [s for s in seats if _row_letter(s["name"]) in watched_rows]
    else:
        pool = seats

    by_row = {}
    for s in pool:
        if s["available"]:
            by_row.setdefault(s["row"], []).append(s)

    groups = []
    for _, row_seats in sorted(by_row.items()):
        row_seats.sort(key=lambda s: s["column"])
        run = []
        for s in row_seats:
            if run and s["column"] == run[-1]["column"] + 1:
                run.append(s)
            else:
                if len(run) >= need:
                    groups.append([x["name"] for x in run])
                run = [s]
        if len(run) >= need:
            groups.append([x["name"] for x in run])
    return groups


def signature(groups):
    """Stable id for a distinct seat-set match, for notify-once dedupe."""
    canonical = sorted(sorted(g) for g in groups)
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()[:16]
