#!/usr/bin/env python3
"""NETWORK screen: internet uptime timeline, outage log, latency.

Two data eras, drawn differently on purpose:
  * bootstrap  -- history.json, mined once out of the Pi-hole FTL DB (134 days).
                  DNS evidence only, minute resolution, no failure classification,
                  and blind to any minute without DNS traffic. Drawn HOLLOW.
  * measured   -- netlog.jsonl / samples.jsonl from netmon.py, 30 s probes with
                  full coverage and classified failure modes. Drawn SOLID.
The day strip therefore shows exactly where real monitoring began instead of
implying 134 days of precision we do not have.

Preview: WK_PNG=1 python3 screens/net/net_panel.py
"""
import os, sys, json, time, datetime, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from zoneinfo import ZoneInfo
from kiosk_common import (W, H, PANEL_W, PAD, ACC, FG, SUB, WARN, ERR, CARD, LINE,
                          FB, FR, F, TZ, new_canvas, header, finish, fit_font, ellip)

MYDIR = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(MYDIR, "history.json")
NETLOG = os.path.join(MYDIR, "netlog.jsonl")
SAMPLES = os.path.join(MYDIR, "samples.jsonl")
LIVE = os.path.join(MYDIR, "live.json")

CYCLE = 30
DAYS = 30                    # width of the day strip
CW = PANEL_W - 2 * PAD       # content width (1000)
ZI = ZoneInfo(TZ)

OK_STATES = {"up", "icmp_blocked"}   # icmp filtered is not an outage
GREEN = (120, 220, 150)
NODATA = (52, 58, 72)
REBOOT = (104, 126, 178)      # planned nightly restart -- known, not "no data"

# root cron reboots the box at 05:00 every night and the kiosk comes back at
# 05:03, so netmon is dead for ~1-2 min daily. That gap is EXPECTED and gets its
# own colour instead of the ambiguous grey. A gap only counts as the nightly
# restart if it is short AND lands in this window -- a multi-day shutdown while
# the user is away stays honest grey "no data".
REBOOT_FROM = (4, 50)         # 04:50 local
REBOOT_TO = (5, 25)           # 05:25 local
REBOOT_MAX = 20 * 60          # longer than this is not the nightly reboot

# /usr/local/sbin/wan-reanchor drops the upstream session on purpose at 04:57 every
# night (the 24 h subscriber cap would otherwise fire at a random hour) and the
# uplink is gone for ~60-90 s while it re-DHCPs. netmon sees a real link_down,
# but it is a PLANNED maintenance hole, so it must not read as an incident:
# excluded from the outage log, the "N in 30 days" count, the day-strip red and
# the 30-day uptime figure. It stays visible in the 24 h ribbon and the latency
# graph -- those show what the link actually did, minute by minute.
# Detection reuses wan-reanchor's own `reanchor` marker in netlog.jsonl, exactly
# as /usr/local/bin/wan-report does: a down-transition detected within
# PLANNED_WINDOW seconds after a marker belongs to that run.
PLANNED_WINDOW = 180
TYPE_COL = {
    "wan_down":  ERR,
    "gw_down":   (255, 110, 60),
    "link_down": (190, 120, 255),
    "no_lease":  WARN,
    "dns_down":  ACC,
    "upstream":  ERR,          # bootstrap events cannot be classified further
}
TYPE_LBL = {
    "wan_down":  "upstream uplink dead",
    "gw_down":   "upstream gateway unreachable",
    "link_down": "cable / carrier lost",
    "no_lease":  "no DHCP lease",
    "dns_down":  "resolvers unreachable",
    "upstream":  "no upstream reply",
}


# ---------- loading ----------------------------------------------------------
def jload(p, default=None):
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return default


def jlines(p, since=None):
    out = []
    try:
        with open(p) as f:
            for line in f:
                try:
                    o = json.loads(line)
                except ValueError:
                    continue
                if since is None or o.get("t", 0) >= since:
                    out.append(o)
    except OSError:
        pass
    return out


def measured_outages(events, now):
    """Turn netmon state transitions into closed outage intervals.

    Each interval carries `planned`: True for the nightly wan-reanchor hole
    (see PLANNED_WINDOW). netlog is not chronological -- wan-reanchor only
    appends its marker once the run has finished, so the markers are collected
    in a first pass before any transition is judged.
    """
    reanchors = [e["t"] for e in events if e.get("ev") == "reanchor"]
    out, cur = [], None
    for e in events:
        if e.get("ev") == "state":
            if cur:
                cur["end"] = e.get("since", e["t"])
                out.append(cur)
                cur = None
            if e.get("to") not in OK_STATES:
                cur = {"start": e.get("since", e["t"]), "type": e["to"], "src": "live",
                       "planned": any(0 <= e["t"] - r <= PLANNED_WINDOW for r in reanchors)}
        elif e.get("ev") == "stop" and cur:
            cur["end"] = e["t"]
            out.append(cur)
            cur = None
    if cur:
        cur["end"] = now
        cur["ongoing"] = True
        out.append(cur)
    for o in out:
        o["dur_min"] = max(1, int(round((o["end"] - o["start"]) / 60.0)))
    return out


def in_reboot_window(t):
    lt = datetime.datetime.fromtimestamp(t, ZI)
    hm = (lt.hour, lt.minute)
    return REBOOT_FROM <= hm <= REBOOT_TO


def gaps(samples, log, now):
    """Stretches with no probes at all -> [(start, end, kind)].

    kind is "reboot" for the expected nightly restart, else "nodata". Gaps come
    from holes in the sample stream; a clean stop/start pair (systemd SIGTERM at
    reboot) is used to tighten the edges when available.
    """
    ts = sorted(s["t"] for s in samples)
    out = []
    for a, b in zip(ts, ts[1:]):
        if b - a <= CYCLE * 3:
            continue
        kind = "nodata"
        if b - a <= REBOOT_MAX and (in_reboot_window(a) or in_reboot_window(b)):
            kind = "reboot"
        out.append((a + CYCLE, b, kind))
    # a gap that is still open (monitor died and never came back)
    if ts and now - ts[-1] > CYCLE * 3:
        out.append((ts[-1] + CYCLE, now, "nodata"))
    return out


def coverage_by_day(events, samples):
    """{date: (covered_s, down_s)} from netmon's hourly rollups.

    The rollup for the hour in progress is not written yet, so the current hour
    is counted straight from the samples -- otherwise today's bar sits grey for
    up to an hour after every restart and after every midnight.
    """
    cov = {}
    last_h = 0
    for e in events:
        if e.get("ev") != "hour":
            continue
        last_h = max(last_h, e["h"])
        d = datetime.date.fromtimestamp(e["h"]).isoformat()
        c, dn = cov.get(d, (0, 0))
        cov[d] = (c + e.get("probes", 0) * CYCLE, dn + e.get("down_s", 0))
    for s in samples:
        if s["t"] < last_h + 3600:
            continue
        d = datetime.date.fromtimestamp(s["t"]).isoformat()
        c, dn = cov.get(d, (0, 0))
        cov[d] = (c + CYCLE, dn + (0 if s.get("s") in OK_STATES else CYCLE))
    return cov


def planned_by_day(outs):
    """{date: seconds} of planned downtime, in the hour-rollup's own units.

    coverage_by_day() reads netmon's aggregated `down_s`, which counts one whole
    CYCLE per down probe -- so the raw wall-clock length of the hole (58 s) would
    leave a 2 s residue behind, and the day strip draws a 6 px minimum red bar
    for ANY non-zero downtime. Rounding up to the probe grid is EXACT, not an
    approximation: netmon sets the outage start to the first down probe and the
    end to the first probe that came back, so ceil(span / CYCLE) is precisely the
    number of down probes the rollup counted (verified 60/60/90/60 s over the
    first four nights). Deliberately no extra slack cycle -- that would also
    swallow one genuine single-probe blip on the same day, and those SHOULD show.
    """
    per = {}
    for o in outs:
        if not o.get("planned"):
            continue
        secs = int(math.ceil((o["end"] - o["start"]) / float(CYCLE))) * CYCLE
        k = datetime.date.fromtimestamp(o["start"]).isoformat()
        per[k] = per.get(k, 0) + secs
    return per


def streak_line(state, stale, live, real, mon_start, now):
    """(seconds, label) for the hero's right-hand figure.

    While the link is UP this answers "how long since the last REAL outage", not
    "how long has netmon been running". netmon's `since` is a per-process
    variable -- the nightly reboot and the 04:57 re-anchor both restart the
    service, so the live streak resets at ~05:01 daily and could never pass 24 h
    even on a perfectly quiet week. netlog is append-only, so anchoring on the
    end of the last non-planned outage survives both, and keeps this widget
    consistent with the strip / uptime % / outage log, which all ignore planned
    events too.

    While an outage is ONGOING the live value is the interesting one instead:
    that is how long the current break has lasted.
    """
    if stale:
        return None, None
    if state in OK_STATES:
        ends = [o["end"] for o in real if not o.get("ongoing")]
        if ends:
            return now - max(ends), "since last outage"
        if mon_start:                      # nothing has ever broken on record
            return now - mon_start, "since monitoring began"
        return None, None
    if live.get("since"):
        return now - live["since"], "in this state"
    return None, None


# ---------- drawing helpers -------------------------------------------------
def rrect(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle(box, r, fill=fill, outline=outline, width=width)


def dur_txt(mins):
    if mins < 60:
        return "%dm" % mins
    if mins < 1440:
        return "%dh %02dm" % (mins // 60, mins % 60)
    return "%dd %dh" % (mins // 1440, (mins % 1440) // 60)


def uptime_txt(sec):
    m = int(sec // 60)
    if m < 60:
        return "%dm" % m
    if m < 1440:
        return "%dh %02dm" % (m // 60, m % 60)
    return "%dd %dh" % (m // 1440, (m % 1440) // 60)


# ---------- main ------------------------------------------------------------
def main():
    now = int(time.time())
    hist = jload(HIST, {}) or {}
    live = jload(LIVE, {}) or {}
    log = jlines(NETLOG)
    samples = jlines(SAMPLES, since=now - 86400)

    mon_start = next((e["t"] for e in log if e.get("ev") == "start"), None)
    cov = coverage_by_day(log, samples)
    gap_list = gaps(samples, log, now)
    live_out = measured_outages(log, now)
    planned = planned_by_day(live_out)
    boot_out = hist.get("events", [])
    # the incidents worth reporting: everything except the planned nightly
    # re-anchor. Used by the hero streak AND the outage log below.
    real = [o for o in live_out if not o.get("planned")] + boot_out
    boot_days = hist.get("days", {})

    img, d = new_canvas()
    header(d, "Network")

    # --- status hero (kept clear of the CPU-temp overlay band, y84-148) ------
    state = live.get("state") or "unknown"
    stale = (now - live.get("t", 0)) > 180 if live else True
    if stale:
        state = "unknown"
    col = GREEN if state in OK_STATES else TYPE_COL.get(state, SUB)
    label = {"up": "Online", "icmp_blocked": "Online · ICMP filtered",
             "unknown": "Monitor not reporting"}.get(state, TYPE_LBL.get(state, state))

    y = 168
    d.ellipse([PAD, y + 12, PAD + 22, y + 34], fill=col)
    f1 = fit_font(d, label, FB, 52, 30, CW - 380)
    d.text((PAD + 40, y), label, font=f1, fill=col)
    ssec, slbl = streak_line(state, stale, live, real, mon_start, now)
    if ssec is not None:
        d.text((PANEL_W - PAD, y + 6), uptime_txt(ssec),
               font=F(FB, 44), fill=FG, anchor="ra")
        d.text((PANEL_W - PAD, y + 58), slbl, font=F(FR, 24), fill=SUB, anchor="ra")

    # --- 30-day uptime figure ----------------------------------------------
    today = datetime.date.today()
    days = [today - datetime.timedelta(days=i) for i in range(DAYS - 1, -1, -1)]

    # A day can carry BOTH kinds of evidence -- netmon started mid-day, and the
    # frozen bootstrap still holds that morning's outages. Merge additively;
    # picking one source would silently drop the other's outages.
    per_day, tot_down, tot_span, n_meas = {}, 0, 0, 0
    for dt in days:
        k = dt.isoformat()
        c, dn = cov.get(k, (0, 0))
        b = boot_days.get(k)
        if b:
            dn += b["down"] * 60
        dn = max(0, dn - planned.get(k, 0))   # nightly re-anchor is not an outage
        span = 86400 if dt != today else max(1, now - int(
            datetime.datetime.combine(dt, datetime.time(), ZI).timestamp()))
        if c:
            n_meas += 1
            base = max(c, b["obs"] * 60 if b else 0)
        else:
            base = span if b else 0
        per_day[k] = {"down": dn, "base": base, "meas": bool(c), "boot": bool(b)}
        tot_down += dn
        tot_span += base
    pct = 100.0 * (1 - tot_down / tot_span) if tot_span else None

    y = 262
    d.text((PAD, y), ("%.3f%%" % pct) if pct is not None else "—",
           font=F(FB, 76), fill=GREEN if (pct or 0) >= 99.5 else WARN)
    sub = "uptime · %d days" % DAYS
    if n_meas and n_meas < DAYS:
        sub += "   (%d measured, %d from Pi-hole logs)" % (n_meas, DAYS - n_meas)
    elif not n_meas:
        sub += "   (from Pi-hole logs)"
    d.text((PAD + 8, y + 86), sub, font=F(FR, 26), fill=SUB)
    if tot_down:
        d.text((PANEL_W - PAD, y + 24), dur_txt(int(tot_down / 60)) + " down",
               font=F(FB, 34), fill=SUB, anchor="ra")

    # --- day strip ----------------------------------------------------------
    y = 414
    bw = CW / DAYS
    d.text((PAD, y - 30), "Last %d days" % DAYS, font=F(FR, 24), fill=SUB)
    d.text((PANEL_W - PAD, y - 28), "solid = measured · hollow = Pi-hole log",
           font=F(FR, 20), fill=SUB, anchor="ra")
    for i, dt in enumerate(days):
        k = dt.isoformat()
        e = per_day[k]
        x0 = PAD + i * bw
        box = [x0 + 1, y, x0 + bw - 3, y + 54]
        frac = e["down"] / e["base"] if e["base"] else 0
        if not e["base"]:
            rrect(d, box, 4, fill=NODATA)
        elif e["meas"]:
            rrect(d, box, 4, fill=CARD)
            if frac > 0:
                hgt = max(6, int(54 * min(1.0, frac * 8)))   # amplify: a 1% day must be visible
                rrect(d, [box[0], box[3] - hgt, box[2], box[3]], 4, fill=ERR)
                rrect(d, [box[0], box[1], box[2], box[3] - hgt], 4, fill=GREEN)
            else:
                rrect(d, box, 4, fill=GREEN)
        else:
            # hollow = bootstrap era: evidence, not measurement
            rrect(d, box, 4, outline=ERR if e["down"] else GREEN, width=2)
        if dt.day == 1 or i == 0 or i == DAYS - 1:
            d.text((x0 + bw / 2, y + 58), dt.strftime("%d"), font=F(FR, 20),
                   fill=SUB, anchor="ma")

    # --- 24 h ribbon --------------------------------------------------------
    y = 536
    d.text((PAD, y - 30), "Last 24 hours", font=F(FR, 24), fill=SUB)
    cols = 250                                   # ~5.8 min per column
    slot = 86400 / cols
    worst = [None] * cols
    for s in samples:
        i = int((s["t"] - (now - 86400)) / slot)
        if 0 <= i < cols:
            st = s.get("s")
            if worst[i] is None or (st not in OK_STATES and worst[i] in OK_STATES):
                worst[i] = st
    # label the empty columns: expected nightly restart vs genuinely unknown
    kind = [None] * cols
    for a, b, k in gap_list:
        for i in range(max(0, int((a - (now - 86400)) / slot)),
                       min(cols, int(math.ceil((b - (now - 86400)) / slot)))):
            if worst[i] is None:
                kind[i] = k
    cwid = CW / cols
    for i, st in enumerate(worst):
        x0 = PAD + i * cwid
        if st is None:
            c = REBOOT if kind[i] == "reboot" else NODATA
        else:
            c = GREEN if st in OK_STATES else TYPE_COL.get(st, ERR)
        d.rectangle([x0, y, x0 + cwid + 0.6, y + 34], fill=c)
    # The nightly restart is only ~1-2 min -- shorter than one column -- so it
    # would vanish into the neighbouring green. Draw it at its true position
    # with a minimum width instead of letting the bucketing eat it.
    has_reboot = False
    for a, b, k in gap_list:
        if k != "reboot" or b < now - 86400:
            continue
        x0 = PAD + max(0.0, (a - (now - 86400)) / 86400.0) * CW
        x1 = PAD + min(1.0, (b - (now - 86400)) / 86400.0) * CW
        d.rectangle([x0, y, max(x1, x0 + 4), y + 34], fill=REBOOT)
        has_reboot = True

    d.text((PAD, y + 42), (datetime.datetime.fromtimestamp(now - 86400, ZI)
                           .strftime("%a %H:%M")), font=F(FR, 20), fill=SUB)
    d.text((PANEL_W - PAD, y + 42), "now", font=F(FR, 20), fill=SUB, anchor="ra")
    # legend, only for the kinds actually on screen right now
    seen = [(REBOOT, "nightly restart"), (NODATA, "no data")]
    seen = [(c, t) for c, t in seen
            if (t == "nightly restart" and has_reboot)
            or (t == "no data" and any(k != "reboot" and worst[i] is None
                                       for i, k in enumerate(kind)))]
    if seen:
        wid = sum(24 + d.textlength(t, font=F(FR, 20)) + 26 for _, t in seen) - 26
        lx = PAD + (CW - wid) / 2
        for c, t in seen:
            d.rectangle([lx, y + 47, lx + 14, y + 61], fill=c)
            d.text((lx + 24, y + 42), t, font=F(FR, 20), fill=SUB)
            lx += 24 + d.textlength(t, font=F(FR, 20)) + 26

    # --- latency sparkline --------------------------------------------------
    y = 648
    rtts = [s["r"] for s in samples if s.get("r") is not None]
    d.text((PAD, y - 30), "Latency 24 h", font=F(FR, 24), fill=SUB)
    if len(rtts) >= 10:
        srt = sorted(rtts)
        p95 = srt[min(len(srt) - 1, int(len(srt) * 0.95))]
        avg = sum(rtts) / len(rtts)
        loss = 100.0 * sum(1 for s in samples if s.get("r") is None) / max(1, len(samples))
        top = max(10.0, p95 * 1.6)
        gh = 96
        rrect(d, [PAD, y, PAD + CW, y + gh], 6, fill=(18, 22, 32))
        # per-column median so 2880 samples compress without spikes dominating
        buck = [[] for _ in range(cols)]
        for s in samples:
            if s.get("r") is None:
                continue
            i = int((s["t"] - (now - 86400)) / slot)
            if 0 <= i < cols:
                buck[i].append(s["r"])
        pts = []
        for i, b in enumerate(buck):
            if not b:
                continue
            b.sort()
            v = b[len(b) // 2]
            pts.append((PAD + i * cwid + cwid / 2,
                        y + gh - min(gh - 2, (min(v, top) / top) * (gh - 2))))
        if len(pts) > 1:
            d.line(pts, fill=ACC, width=2, joint="curve")
        for s in samples:                        # mark probe failures on the graph
            if s.get("r") is None:
                i = int((s["t"] - (now - 86400)) / slot)
                if 0 <= i < cols:
                    x0 = PAD + i * cwid
                    d.rectangle([x0, y, x0 + max(1.5, cwid), y + gh], fill=(90, 30, 36))
        d.text((PAD + 10, y + gh - 30), "%.0f ms" % top, font=F(FR, 20), fill=SUB)
        info = "now %s · avg %.1f · p95 %.0f ms · loss %.2f%%" % (
            ("%.1f" % live["rtt"]) if live.get("rtt") is not None else "—", avg, p95, loss)
        d.text((PANEL_W - PAD, y - 30), info, font=F(FR, 24), fill=SUB, anchor="ra")
    else:
        rrect(d, [PAD, y, PAD + CW, y + 96], 6, fill=(18, 22, 32))
        d.text((PAD + CW / 2, y + 34), "collecting…", font=F(FR, 28), fill=SUB, anchor="ma")

    # --- recent outages -----------------------------------------------------
    # Everything below here must stay above y=1022: finish() paints the global
    # red "No internet connection" banner over that strip, and it would cover
    # the footer exactly when an outage makes this screen worth reading.
    y = 758
    d.text((PAD, y), "Recent outages", font=F(FB, 34), fill=FG)
    # 4 rows: 5 would run into the footer at y=1000. `real` excludes the planned
    # nightly re-anchor holes -- expected maintenance, and listing one every
    # morning buried the outages worth reading.
    allout = sorted(real, key=lambda o: -o["start"])[:4]
    n30 = sum(1 for o in real if o["start"] >= now - DAYS * 86400)
    d.text((PANEL_W - PAD, y + 8), "%d in %d days" % (n30, DAYS),
           font=F(FR, 24), fill=SUB, anchor="ra")
    ry = y + 48
    if not allout:
        d.text((PAD, ry + 6), "None on record — nothing to show yet.",
               font=F(FR, 30), fill=SUB)
    for o in allout:
        c = TYPE_COL.get(o["type"], ERR)
        boot = o.get("src") == "ftl"
        st = datetime.datetime.fromtimestamp(o["start"], ZI)
        en = datetime.datetime.fromtimestamp(o["end"], ZI)
        if boot:
            d.ellipse([PAD, ry + 12, PAD + 16, ry + 28], outline=c, width=2)
        else:
            d.ellipse([PAD, ry + 12, PAD + 16, ry + 28], fill=c)
        d.text((PAD + 30, ry), st.strftime("%a %d %b"), font=F(FB, 30), fill=FG)
        d.text((PAD + 220, ry), "%s – %s" % (st.strftime("%H:%M"), en.strftime("%H:%M")),
               font=F(FR, 30), fill=SUB)
        d.text((PAD + 462, ry), dur_txt(o["dur_min"]) + ("  (ongoing)" if o.get("ongoing") else ""),
               font=F(FB, 30), fill=c)
        # no marker needed for bootstrap rows: the hollow dot + strip legend say it
        lbl = TYPE_LBL.get(o["type"], o["type"])
        d.text((PANEL_W - PAD, ry + 2), ellip(d, lbl, F(FR, 26), 340),
               font=F(FR, 26), fill=SUB, anchor="ra")
        ry += 42

    # --- footer (must end above the y=1022 offline-banner strip) -------------
    fy = H - 100
    d.line([PAD, fy - 14, PANEL_W - PAD, fy - 14], fill=LINE, width=1)
    wan = live.get("ip") or "—"
    nip = sum(1 for e in log if e.get("ev") == "wanip")
    left = "WAN %s" % wan
    if nip:
        left += " · %d IP change%s" % (nip, "" if nip == 1 else "s")
    d.text((PAD, fy), left, font=F(FR, 24), fill=SUB)
    if mon_start:
        since = datetime.datetime.fromtimestamp(mon_start, ZI).strftime("%d %b")
        right = "30 s probes since %s · log from %s" % (
            since, datetime.datetime.fromtimestamp(
                hist.get("span", [now])[0], ZI).strftime("%d %b") if hist.get("span") else "—")
    else:
        right = "monitor offline"
    d.text((PANEL_W - PAD, fy), right, font=F(FR, 24), fill=SUB, anchor="ra")

    finish("net", img, MYDIR)


if __name__ == "__main__":
    main()
