#!/usr/bin/env python3
"""ONE-OFF bootstrap: mine outage history out of the Pi-hole FTL long-term DB.

Run this exactly once to give the Network screen a populated timeline on day one.
It is NOT part of the ongoing pipeline -- netmon.py owns everything from now on.
Output: history.json next to this script.

Detection logic: a forwarded query (status=2) whose reply_time is NULL never got an
answer from any upstream. A minute where most forwards went unanswered means the
path to the internet was broken. Thresholds (unans>=5, ratio>=0.6) were picked by
inspecting the real data: the DB is full of 1/2 and 3/6 minutes (ratio exactly 0.5,
tiny counts, many at a recurring 22:24) which are a flush race / single-upstream
retry, not outages.

Coverage caveat, deliberately preserved in the output: a minute with no DNS traffic
carries no evidence either way. Only minutes with >=3 forwards count as observed,
and uptime must be computed against those -- not against wall-clock.
"""
import sqlite3, json, os, time, datetime, calendar

DB = "/data/compose/4/etc-pihole/pihole-FTL.db"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")

MIN_UNANS = 5     # absolute floor, kills the 1/2 + 3/6 noise class
MIN_RATIO = 0.6   # share of forwards left unanswered
MERGE_GAP = 3     # minutes of recovery tolerated inside one event
OBS_FWD = 3       # forwards/minute needed to call the minute "observed"
NODATA_MIN = 60   # a zero-query stretch this long is the Pi itself being down

c = sqlite3.connect("file:%s?mode=ro" % DB, uri=True)

fwd, unans = {}, {}
for m, k, u in c.execute(
    "select cast(timestamp/60 as int),count(*),sum(reply_time is null) "
    "from query_storage where status=2 group by 1"):
    fwd[m] = k
    unans[m] = u or 0
anyq = dict(c.execute(
    "select cast(timestamp/60 as int),count(*) from query_storage group by 1"))

lo, hi = min(anyq), max(anyq)

# --- outage events -----------------------------------------------------------
bad = sorted(m for m in fwd if unans[m] >= MIN_UNANS and unans[m] / fwd[m] >= MIN_RATIO)
runs = []
for m in bad:
    if runs and m - runs[-1][1] <= MERGE_GAP:
        runs[-1][1] = m
    else:
        runs.append([m, m])

events = []
for a, b in runs:
    rng = range(a, b + 1)
    events.append({
        "start": a * 60,
        "end": (b + 1) * 60,
        "dur_min": b - a + 1,
        # the DB can only ever prove "no upstream answered" -- it cannot tell
        # upstream-link-down from gateway-down from ISP-down. netmon can; this can't.
        "type": "upstream",
        "src": "ftl",
        "unans": sum(unans.get(m, 0) for m in rng),
        "fwd": sum(fwd.get(m, 0) for m in rng),
    })

# --- no-data blocks (Pi off / DB not writing), long ones only ----------------
nodata, prev = [], None
for m in sorted(anyq):
    if prev is not None and m - prev - 1 >= NODATA_MIN:
        nodata.append({"start": (prev + 1) * 60, "end": m * 60,
                       "dur_min": m - prev - 1, "src": "ftl"})
    prev = m

# --- per-day observed / down minutes ----------------------------------------
down_min = set()
for a, b in runs:
    down_min.update(range(a, b + 1))

days = {}
for m in range(lo, hi + 1):
    d = datetime.date.fromtimestamp(m * 60).isoformat()
    e = days.setdefault(d, {"obs": 0, "down": 0})
    if m in down_min:
        e["down"] += 1
        e["obs"] += 1          # an outage minute is by definition observed
    elif fwd.get(m, 0) >= OBS_FWD:
        e["obs"] += 1

# --- DNS upstream resolve time per day (context only; NOT the ICMP series) ---
dns_daily = {}
for d, n, avg, mx in c.execute(
    'select date(timestamp,"unixepoch","localtime") d,count(*),avg(reply_time),max(reply_time) '
    'from query_storage where status=2 and reply_time is not null group by d'):
    dns_daily[d] = {"n": n, "avg": round(avg, 4), "max": round(mx, 2)}

obs = sum(v["obs"] for v in days.values())
dwn = sum(v["down"] for v in days.values())

hist = {
    "generated": int(time.time()),
    "source": "pihole-FTL.db one-off bootstrap (frozen, not updated)",
    "note": "DNS-evidence only. Minutes without DNS traffic are unobserved, "
            "not up. Uptime is over observed minutes.",
    "span": [lo * 60, (hi + 1) * 60],
    "observed_min": obs,
    "down_min": dwn,
    "events": events,
    "nodata": nodata,
    "days": days,
    "dns_daily": dns_daily,
}

tmp = OUT + ".tmp"
with open(tmp, "w") as f:
    json.dump(hist, f, separators=(",", ":"))
os.replace(tmp, OUT)

f = lambda t: datetime.datetime.fromtimestamp(t).strftime("%a %d %b %H:%M")
print("wrote %s (%d bytes)" % (OUT, os.path.getsize(OUT)))
print("span      %s -> %s (%.1f d)" % (f(lo * 60), f(hi * 60), (hi - lo) / 1440))
print("events    %d  (%d min down)" % (len(events), dwn))
print("observed  %d min of %d wall-clock (%.1f%% coverage)"
      % (obs, hi - lo + 1, 100 * obs / (hi - lo + 1)))
print("uptime    %.4f%% of observed minutes" % (100 * (1 - dwn / obs)))
print("nodata    %d block(s): %s" % (len(nodata),
      ", ".join("%s %dm" % (f(x["start"]), x["dur_min"]) for x in nodata)))
print("days      %d" % len(days))
print("worst 6:")
for e in sorted(events, key=lambda e: -e["dur_min"])[:6]:
    print("   %-20s %4dm  %d/%d unanswered" % (f(e["start"]), e["dur_min"], e["unans"], e["fwd"]))
