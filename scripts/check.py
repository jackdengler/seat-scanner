"""Polling entrypoint, run by the poll workflow every 5 minutes.

Reads config.json (watches, written by the UI) and subscriptions.json
(push subscriptions, written by the PWA) from the default branch, and
state.json plus per-showtime seatmaps from the data branch checked out
at the path given by --data.

Check cadence per watch, by time until the showtime:
    more than 7 days        every 3 hours
    1 to 7 days             every 15 minutes
    8 to 24 hours           every 5 minutes (every cron run)
    last 8 hours            every run, plus an in-run burst (~30s, jittered)
    showtime passed         mark done, stop checking

The 5-minute cron is a floor; inside the final 8 hours one run loops
several extra times with randomized spacing so the effective cadence is
~30 seconds, and runs self-chain (see CHAIN below) so coverage stays
continuous even when GitHub's cron skips a tick. The jitter keeps the
pattern from looking robotic/fingerprintable.

Notifies at most once per distinct seat-set match, and sends a
"watcher broken" alert after 3 consecutive fetch failures (once).
Exits 0 always unless inputs are unreadable; prints ALL_DONE=true when
no active watches remain so the workflow can disable itself.
"""

import argparse
import datetime
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amc
import matcher

FAILURE_ALERT_THRESHOLD = 3
CRON_SLACK_MIN = 1  # cron jitter allowance when deciding if a check is due

# In the final hours the cron's 5-minute floor isn't fast enough, so a
# single run loops many times with randomized spacing — as responsive as
# Actions practically allows, without a robotic, easily-fingerprinted
# cadence. Kept just under the 5-min tick so the run ends before the next
# scheduled one, and the self-chain keeps a fresh run always queued.
BURST_WINDOW_MIN = 8 * 60      # only burst when a watch is within this window
BURST_MAX_SECONDS = 270        # stop bursting before the next 5-min cron tick
BURST_SLEEP_RANGE = (25, 40)   # jittered seconds between passes (~30s)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


def parse_iso(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
        f.write("\n")


def interval_minutes(minutes_to_show):
    if minutes_to_show <= 8 * 60:
        return 0          # every run (plus in-run burst)
    if minutes_to_show <= 24 * 60:
        return 5
    if minutes_to_show <= 7 * 24 * 60:
        return 15
    return 3 * 60


def is_due(ws, minutes_to_show, now):
    last = ws.get("lastCheckedAt")
    if not last:
        return True
    interval = interval_minutes(minutes_to_show)
    elapsed = (now - parse_iso(last)).total_seconds() / 60
    return elapsed >= interval - CRON_SLACK_MIN


def push(subscriptions, payload, state):
    key = os.environ.get("VAPID_PRIVATE_KEY", "")
    if not key:
        print("VAPID_PRIVATE_KEY not set; skipping push:", payload.get("title"))
        return
    if not subscriptions:
        print("no push subscriptions registered; skipping push:",
              payload.get("title"))
        return
    import notify_push
    dead = notify_push.send_all(subscriptions, payload, key)
    if dead:
        existing = set(state.setdefault("deadSubscriptions", []))
        state["deadSubscriptions"] = sorted(existing | set(dead))


def check_watch(watch, ws, subscriptions, state, data_dir, now):
    sid = str(watch["showtimeId"])
    label = watch.get("label") or f"showtime {sid}"

    show_at = parse_iso(watch["showtimeIso"])
    minutes_to_show = (show_at - now).total_seconds() / 60
    if minutes_to_show <= 0:
        print(f"[{sid}] showtime passed; marking done")
        ws["done"] = True
        return

    if not is_due(ws, minutes_to_show, now):
        print(f"[{sid}] not due yet "
              f"(tier interval {interval_minutes(minutes_to_show)}min)")
        return

    try:
        seatmap = amc.fetch_seatmap(sid, log=lambda m: print(f"[{sid}] {m}"))
    except Exception as e:
        ws["consecutiveFailures"] = ws.get("consecutiveFailures", 0) + 1
        print(f"[{sid}] fetch failed ({ws['consecutiveFailures']}x): {e}")
        if (ws["consecutiveFailures"] >= FAILURE_ALERT_THRESHOLD
                and not ws.get("alertedBroken")):
            push(subscriptions, {
                "title": "Seat watcher broken",
                "body": (f"{label}: {ws['consecutiveFailures']} fetches in a row "
                         f"failed ({e}). Check the Actions logs."),
                "tag": f"broken-{sid}",
            }, state)
            ws["alertedBroken"] = True
        return

    ws["lastCheckedAt"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ws["consecutiveFailures"] = 0
    ws["alertedBroken"] = False
    save_json(os.path.join(data_dir, f"seatmap-{sid}.json"), seatmap)

    groups = matcher.evaluate(watch, seatmap)
    avail = sum(1 for s in seatmap["seats"] if s["available"])
    print(f"[{sid}] {avail}/{len(seatmap['seats'])} seats available, "
          f"{len(groups)} qualifying group(s)")
    if not groups:
        return

    sig = matcher.signature(groups)
    if sig in ws.get("notifiedSignatures", []):
        print(f"[{sid}] match {sig} already notified")
        return

    seats_text = "; ".join(", ".join(g) for g in groups)
    push(subscriptions, {
        "title": f"Seats open: {label}",
        "body": f"Available now: {seats_text}",
        "tag": f"match-{sid}-{sig}",
        "url": f"https://www.amctheatres.com/showtimes/{sid}/seats",
    }, state)
    ws.setdefault("notifiedSignatures", []).append(sig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--subscriptions", default="subscriptions.json")
    ap.add_argument("--data", default="data")
    args = ap.parse_args()

    config = load_json(args.config, {"watches": []})
    subscriptions = load_json(args.subscriptions, {"subscriptions": []})[
        "subscriptions"]
    state_path = os.path.join(args.data, "state.json")
    state = load_json(state_path, {"watches": {}})

    def one_pass():
        now = now_utc()
        active = 0
        soonest = None  # minutes to the nearest active showtime
        for watch in config.get("watches", []):
            sid = str(watch.get("showtimeId", ""))
            if not sid or not watch.get("showtimeIso"):
                print(f"skipping malformed watch: {watch}")
                continue
            ws = state["watches"].setdefault(sid, {})
            if ws.get("done") or watch.get("done"):
                continue
            check_watch(watch, ws, subscriptions, state, args.data, now)
            if not ws.get("done"):
                active += 1
                mins = (parse_iso(watch["showtimeIso"]) - now).total_seconds() / 60
                soonest = mins if soonest is None else min(soonest, mins)
        return active, soonest

    active, soonest = one_pass()

    # Burst: keep re-checking within one run while a showtime is imminent.
    deadline = time.monotonic() + BURST_MAX_SECONDS
    while (active and soonest is not None and 0 < soonest <= BURST_WINDOW_MIN
           and time.monotonic() < deadline):
        nap = random.uniform(*BURST_SLEEP_RANGE)
        if time.monotonic() + nap >= deadline:
            break
        print(f"burst: showtime in {soonest:.0f}min; re-checking in {nap:.0f}s")
        time.sleep(nap)
        active, soonest = one_pass()

    save_json(state_path, state)
    # CHAIN tells the workflow to re-dispatch itself: GitHub's cron is too
    # unreliable to carry the final hours, so runs hand off to each other
    # while a showtime is inside the burst window.
    chain = bool(active and soonest is not None
                 and 0 < soonest <= BURST_WINDOW_MIN)
    print(f"CHAIN={'true' if chain else 'false'}")
    print(f"ALL_DONE={'true' if active == 0 else 'false'}")


if __name__ == "__main__":
    main()
