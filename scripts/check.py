"""Polling entrypoint, run by the poll workflow every 5 minutes.

Reads config.json (watches, written by the UI) and subscriptions.json
(push subscriptions, written by the PWA) from the default branch, and
state.json plus per-showtime seatmaps from the data branch checked out
at the path given by --data.

Check cadence: every active watch is checked ~every 15 seconds, the whole
time it's active, regardless of how far off the showtime is. Within a pass all
due shows are fetched concurrently (FETCH_WORKERS), so the interval doesn't
stretch as you watch more shows. A watch stops only once its showtime passes
(it's marked done).

The 5-minute cron is just a floor/heartbeat; each run loops many times with
randomized ~15s spacing, and runs self-chain (see CHAIN below) so a fresh
run is always queued and coverage stays continuous even when GitHub's cron
skips a tick. The jitter keeps the pattern from looking robotic.

NOTE: this is deliberately aggressive — a lot of requests to AMC per watch per
day. Push too hard (small BURST_SLEEP_RANGE / high FETCH_WORKERS with many
watches) and AMC's Cloudflare starts returning 429/challenge pages; those are
treated as transient throttles (is_throttle) and never alerted, but coverage
suffers, so back off if you see lots of them in the logs.

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
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import amc
import matcher

FAILURE_ALERT_THRESHOLD = 3
CRON_SLACK_MIN = 1  # cron jitter allowance when deciding if a check is due

# AMC's protection stack (Cloudflare 429/challenge, Queue-It waiting room,
# temporary access-denied) throttles us under heavy polling. Those are transient
# "back off" signals, not a broken watcher, so they must not trigger alerts.
_THROTTLE_MARKERS = ("429", "cloudflare-challenge", "queue-it",
                     "access-denied", "503", "502")


def is_throttle(exc):
    msg = str(exc).lower()
    return any(m in msg for m in _THROTTLE_MARKERS)

# Flat cadence: every active watch is checked this often, no matter how far
# out the showtime is. 0 = every pass (the burst runs ~every 30s). Bump this
# up (e.g. 5 or 15) to check less aggressively.
CHECK_INTERVAL_MIN = 0

# The cron's 5-minute floor isn't fast enough for ~30s checks, so a single
# run loops many times with randomized spacing and self-chains, keeping a
# fresh run always queued. Kept just under the 5-min tick so the run ends
# before the next scheduled one.
BURST_MAX_SECONDS = 270        # stop bursting before the next 5-min cron tick
BURST_SLEEP_RANGE = (12, 18)   # jittered seconds between passes (~15s)

# Each pass fetches every due show's seat map concurrently, so a pass stays
# ~constant regardless of how many shows are watched (the network dance, not
# CPU, is the cost). Kept modest: too many simultaneous connections from one
# runner IP is what trips AMC's Cloudflare rate limit (HTTP 429).
FETCH_WORKERS = 3

# On a throttle, retry the fetch this many extra times with backoff before
# giving up for the pass. Cloudflare 429s are frequently transient.
THROTTLE_RETRIES = 2

# When a pass gets throttled on more than this fraction of its shows, AMC is
# clearly pushing back — cool off with an extra sleep before the next pass so
# the rate-limit window can reset instead of hammering into the block.
THROTTLE_COOLOFF_RATIO = 0.34
THROTTLE_COOLOFF_SECONDS = 45


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
    # Flat: same interval for every watch regardless of time to showtime.
    return CHECK_INTERVAL_MIN


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


def is_watch_due(watch, ws, now):
    """True if this watch should be fetched now. Marks it done if the show has
    passed. (Kept separate from the fetch so a whole pass can fetch in parallel.)"""
    sid = str(watch["showtimeId"])
    show_at = parse_iso(watch["showtimeIso"])
    minutes_to_show = (show_at - now).total_seconds() / 60
    if minutes_to_show <= 0:
        print(f"[{sid}] showtime passed; marking done")
        ws["done"] = True
        return False
    if not is_due(ws, minutes_to_show, now):
        print(f"[{sid}] not due yet "
              f"(tier interval {interval_minutes(minutes_to_show)}min)")
        return False
    return True


def fetch_watch(sid):
    """Network-only, thread-safe: fetch one seat map, returning it or the
    exception raised (so the caller can process results single-threaded).

    Retries a couple of times on a throttle (429/challenge) with jittered
    backoff — those are often transient, so a brief wait recovers many of them
    rather than losing the show for the whole pass."""
    err = None
    delay = 2.0
    for attempt in range(THROTTLE_RETRIES + 1):
        try:
            return amc.fetch_seatmap(sid, log=lambda m: print(f"[{sid}] {m}"))
        except Exception as e:  # noqa: BLE001 — surfaced to the sequential processor
            err = e
            if not is_throttle(e) or attempt == THROTTLE_RETRIES:
                return err
            time.sleep(delay + random.uniform(0, 1.5))  # jitter desyncs workers
            delay *= 2
    return err


def process_watch(watch, ws, subscriptions, state, data_dir, now, result):
    """Handle one fetch result (seat map or exception). Runs single-threaded, so
    all state mutation and pushes are race-free."""
    sid = str(watch["showtimeId"])
    label = watch.get("label") or f"showtime {sid}"

    if isinstance(result, Exception):
        # A Cloudflare/Queue-It/429 throttle means "slow down", not that this
        # watcher is broken — don't count it toward the broken threshold and
        # never notify for it, or aggressive polling spams "watcher broken".
        if is_throttle(result):
            ws["throttledCount"] = ws.get("throttledCount", 0) + 1
            print(f"[{sid}] throttled ({result}); retrying next pass")
            return
        ws["consecutiveFailures"] = ws.get("consecutiveFailures", 0) + 1
        print(f"[{sid}] fetch failed ({ws['consecutiveFailures']}x): {result}")
        if (ws["consecutiveFailures"] >= FAILURE_ALERT_THRESHOLD
                and not ws.get("alertedBroken")):
            push(subscriptions, {
                "title": "Seat watcher broken",
                "body": (f"{label}: {ws['consecutiveFailures']} fetches in a row "
                         f"failed ({result}). Check the Actions logs."),
                "tag": f"broken-{sid}",
            }, state)
            ws["alertedBroken"] = True
        return
    seatmap = result

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
    # Open our own same-origin interstitial (resolved against the service
    # worker scope) rather than amctheatres.com directly: tapping the button
    # there is a genuine user gesture, which is what lets iOS hand off the
    # universal link to the installed AMC app so you can book right away.
    # Opening the external URL straight from the notification tends to land
    # in a browser tab instead.
    query = urllib.parse.urlencode({
        "sid": sid,
        "label": label,       # already "<movie> — <day> at <time>"
        "seats": seats_text,
    })
    key = f"{sid}-{sig}"
    alert = {
        "key": key,
        "sid": sid,
        "label": label,
        "seats": seats_text,
        "showtimeIso": watch.get("showtimeIso"),
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    push(subscriptions, {
        "title": f"Seats open: {label}",
        "body": f"Available now: {seats_text}",
        "tag": f"match-{sid}-{sig}",
        "url": f"open.html?{query}",
        # Carried so an already-open PWA can render this alert instantly, with no
        # round-trip to the data branch (by which time the seat may be gone).
        "alert": alert,
    }, state)
    ws.setdefault("notifiedSignatures", []).append(sig)

    # Also record it in a durable, newest-first feed the PWA renders on load, so
    # a missed/mis-handled notification tap still shows which show(s) opened and
    # a one-tap link to book. Keyed by (sid, sig) so re-runs don't duplicate.
    alerts = state.setdefault("alerts", [])
    if not any(a.get("key") == key for a in alerts):
        alerts.insert(0, alert)
        del alerts[30:]


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

        # 1) Decide which watches are due (marks passed ones done).
        due = []
        for watch in config.get("watches", []):
            sid = str(watch.get("showtimeId", ""))
            if not sid or not watch.get("showtimeIso"):
                print(f"skipping malformed watch: {watch}")
                continue
            ws = state["watches"].setdefault(sid, {})
            if ws.get("done") or watch.get("done"):
                continue
            if is_watch_due(watch, ws, now):
                due.append((watch, ws, sid))

        # 2) Fetch all due seat maps concurrently — the slow part is the network
        # dance, so this keeps a pass ~constant no matter how many are watched.
        results = {}
        if due:
            with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(due))) as ex:
                futs = {ex.submit(fetch_watch, sid): sid for (_, _, sid) in due}
                for fut in as_completed(futs):
                    results[futs[fut]] = fut.result()

        # 3) Process results single-threaded (matching, pushes, state writes),
        # tracking how heavily AMC throttled us this pass.
        throttled = 0
        for watch, ws, sid in due:
            r = results.get(sid)
            if isinstance(r, Exception) and is_throttle(r):
                throttled += 1
            process_watch(watch, ws, subscriptions, state, args.data, now, r)
        throttle_ratio = throttled / len(due) if due else 0.0
        if throttled:
            print(f"pass: {throttled}/{len(due)} shows throttled "
                  f"({throttle_ratio:.0%})")

        # 4) Count what's still active and how soon the nearest showtime is.
        active = 0
        soonest = None
        for watch in config.get("watches", []):
            sid = str(watch.get("showtimeId", ""))
            if not sid or not watch.get("showtimeIso"):
                continue
            ws = state["watches"].get(sid, {})
            if ws.get("done") or watch.get("done"):
                continue
            active += 1
            mins = (parse_iso(watch["showtimeIso"]) - now).total_seconds() / 60
            soonest = mins if soonest is None else min(soonest, mins)
        return active, soonest, throttle_ratio

    active, soonest, throttle_ratio = one_pass()

    # Burst: keep re-checking within one run as long as any watch is active.
    deadline = time.monotonic() + BURST_MAX_SECONDS
    while active and time.monotonic() < deadline:
        nap = random.uniform(*BURST_SLEEP_RANGE)
        # When AMC is pushing back hard, cool off longer so its rate window can
        # reset instead of hammering into the block.
        if throttle_ratio >= THROTTLE_COOLOFF_RATIO:
            nap += THROTTLE_COOLOFF_SECONDS
            print(f"heavy throttling ({throttle_ratio:.0%}); cooling off")
        if time.monotonic() + nap >= deadline:
            break
        soon = f"{soonest:.0f}min" if soonest is not None else "?"
        print(f"burst: nearest showtime in {soon}; re-checking in {nap:.0f}s")
        time.sleep(nap)
        active, soonest, throttle_ratio = one_pass()

    save_json(state_path, state)
    # CHAIN tells the workflow to re-dispatch itself: GitHub's cron is too
    # unreliable to carry a ~30s cadence, so runs hand off to each other as
    # long as any watch is active.
    chain = bool(active)
    print(f"CHAIN={'true' if chain else 'false'}")
    print(f"ALL_DONE={'true' if active == 0 else 'false'}")


if __name__ == "__main__":
    main()
