#!/usr/bin/env python3
"""Verify the two things that cannot be observed on the live screen yet:
   1. the nightly-reboot gap is classified/coloured as a restart, and a
      multi-day shutdown still reads as honest "no data"
   2. the global red offline banner does not cover the footer

Renders to /tmp only -- np.finish is replaced so the live panel.bgra is untouched.
"""
import sys, os, time, datetime, json
ROOT = "/home/skylab/infoscreen"
sys.path.insert(0, ROOT)
sys.path.insert(0, ROOT + "/screens/net")
import kiosk_common as kc
from PIL import ImageDraw
import net_panel as np

ZI = np.ZI
now = int(time.time())

# ---------- 1. gap classification ----------
def gap_at(hhmm, dur_s, day_off=0):
    d = datetime.date.today() - datetime.timedelta(days=day_off)
    t0 = int(datetime.datetime.combine(
        d, datetime.time(*hhmm), ZI).timestamp())
    return [{"t": t0 - 30, "s": "up", "r": 4.0}, {"t": t0 + dur_s, "s": "up", "r": 4.0}]

cases = [
    ("nightly 05:00 reboot, 90 s", gap_at((5, 0), 90), "reboot"),
    ("05:12 restart, 4 min",       gap_at((5, 12), 240), "reboot"),
    ("3-day shutdown from 05:00",  gap_at((5, 0), 3 * 86400), "nodata"),
    ("14:00 outage-ish, 10 min",   gap_at((14, 0), 600), "nodata"),
    ("04:30, 90 s (outside win)",  gap_at((4, 30), 90), "nodata"),
]
ok = True
for name, smp, want in cases:
    g = np.gaps(smp, [], smp[-1]["t"] + 30)
    got = g[0][2] if g else "none"
    flag = "OK " if got == want else "FAIL"
    if got != want:
        ok = False
    print("%s  %-30s want=%-7s got=%s" % (flag, name, want, got))
print("gap classification:", "PASS" if ok else "FAIL")

# ---------- 1b. planned wan-reanchor hole is not an outage ----------
# 04:57 re-anchor: marker at t0, link_down detected 45 s later, back after 60 s.
t0 = now - 4000
log = [
    {"ev": "state", "t": t0 - 600, "to": "up", "since": t0 - 630},
    {"ev": "state", "t": t0 + 45, "to": "link_down", "since": t0 + 15},
    {"ev": "reanchor", "t": t0, "hold": 60, "dur": 63, "online": 1},   # appended late
    {"ev": "state", "t": t0 + 105, "to": "up", "since": t0 + 75},
    # an unrelated outage 3 h later must stay
    {"ev": "state", "t": t0 + 11000, "to": "gw_down", "since": t0 + 10970},
    {"ev": "state", "t": t0 + 12000, "to": "up", "since": t0 + 11970},
]
outs = np.measured_outages(log, now)
flags = [(o["type"], o.get("planned")) for o in outs]
want = [("link_down", True), ("gw_down", False)]
print(("OK  " if flags == want else "FAIL") + "  planned flagging  want=%s got=%s" % (want, flags))
pd = np.planned_by_day(outs)
day = datetime.date.fromtimestamp(t0 + 15).isoformat()
# hole spans 60 s (first down probe -> first probe back) = 2 down probes = 60 s
# of rollup downtime. No slack cycle: a real 1-probe blip the same day must survive.
print(("OK  " if pd == {day: 60} else "FAIL") + "  planned seconds   want={%s: 60} got=%s" % (day, pd))
print("planned classification:", "PASS" if flags == want and pd == {day: 60} else "FAIL")

# ---------- 1c. hero streak: "since last outage" vs "in this state" ----------
# streak_line takes the ALREADY planned-filtered list (main passes `real`), so
# the planned hole never anchors it -- only the real gw_down 3 h later does.
mon = now - 20 * 86400
# a real outage that ended 3 d ago + the planned hole from 1b, which must lose:
# the caller (main) hands streak_line the already-filtered list.
LAST_END = now - 3 * 86400
real_outs = [{"start": LAST_END - 1080, "end": LAST_END, "type": "gw_down"}]
scases = [
    ("up, real outage on record", "up", False, {"since": now - 3600},
     (3 * 86400, "since last outage"), real_outs),
    ("up, nothing real on record", "up", False, {"since": now - 3600},
     (now - mon, "since monitoring began"), []),
    ("down -> live value",         "gw_down", False, {"since": now - 240},
     (240, "in this state")),
    ("monitor stale",              "unknown", True, {"since": now - 240},
     (None, None)),
]
sok = True
for case in scases:
    name, st, stale_f, lv, want = case[:5]
    rl = case[5] if len(case) > 5 else real_outs
    got = np.streak_line(st, stale_f, lv, rl, mon, now)
    if got != want:
        sok = False
    print(("OK  " if got == want else "FAIL") + "  %-26s want=%s got=%s" % (name, want, got))
print("streak line:", "PASS" if sok else "FAIL")

# ---------- 1d. day-strip tiering: red only when there IS an outage row ----------
# A single failed probe (30 s) never becomes a `state` transition (netmon needs
# CONFIRM=2), so it can never appear in "Recent outages" -- it must therefore not
# paint the day red either, or the strip shows an incident nothing explains.
DAY = datetime.date.today()
D0 = DAY.isoformat()
D1 = (DAY - datetime.timedelta(days=1)).isoformat()
mid = int(datetime.datetime.combine(DAY, datetime.time(0, 0), ZI).timestamp())

# an outage that starts before midnight and ends after it must colour BOTH days
odays = np.outage_days([{"start": mid - 600, "end": mid + 600, "type": "gw_down"}])
print(("OK  " if odays == {D1, D0} else "FAIL")
      + "  midnight-spanning outage  want=%s got=%s" % ({D1, D0}, odays))

tcases = [
    ("real outage day",      D0, {"meas": True, "down": 1320}, {D0}, "outage"),
    ("single 30 s blip",     D0, {"meas": True, "down": 30},   set(), "blip"),
    ("two blips, still amber", D0, {"meas": True, "down": 60}, set(), "blip"),
    ("clean day",            D0, {"meas": True, "down": 0},    set(), "none"),
    ("planned-only day",     D0, {"meas": True, "down": 0},    set(), "none"),
    ("bootstrap (hollow)",   D0, {"meas": False, "down": 600}, {D0},  "none"),
]
tok = odays == {D1, D0}
for name, day, entry, od, want in tcases:
    got = np.strip_tier(day, entry, od)
    if got != want:
        tok = False
    print(("OK  " if got == want else "FAIL") + "  %-24s want=%-7s got=%s" % (name, want, got))
# the invariant itself: outage tier implies the day is in the outage-log days
inv = all(np.strip_tier(D0, {"meas": True, "down": n}, set()) != "outage"
          for n in (30, 60, 3600, 86400))
print(("OK  " if inv else "FAIL") + "  no outage-log entry -> never red (any duration)")
print("day-strip tiering:", "PASS" if tok and inv else "FAIL")

# ---------- 2. render with a reboot gap + an outage + offline banner ----------
smp = []
t = now - 86400
reb0 = int(datetime.datetime.combine(datetime.date.today(),
                                     datetime.time(5, 0), ZI).timestamp())
if reb0 > now:                      # 05:00 today hasn't happened yet -> use yesterday's
    reb0 -= 86400
out0, out1 = now - 40000, now - 39100        # a fake 15 min outage
while t < now:
    if reb0 <= t < reb0 + 100:               # nightly reboot: no probes at all
        t += 30
        continue
    down = out0 <= t < out1
    smp.append({"t": t, "s": "wan_down" if down else "up",
                "r": None if down else round(3.5 + (t % 17) / 10.0, 2),
                "g": 2.6, "d": 4.1})
    t += 30

_real_jlines = np.jlines
np.jlines = lambda p, since=None: (smp if p == np.SAMPLES else _real_jlines(p, since))
kc.net_online = lambda ttl=180: (False, now - 3600)   # report offline (see fake_finish)


def fake_finish(key, img, outdir):
    # The offline banner is an OVERLAY now (banner.py -> banner.bgra), not baked in by
    # finish(), so kc.draw_offline_banner() no longer exists. Paint an equivalent strip
    # here (same geometry and text helper banner.py uses) so the test still shows whether
    # the net panel footer collides with the y1022-1080 band the overlay covers -- which is
    # the whole reason this render gets dumped to a PNG.
    d = ImageDraw.Draw(img)
    d.rectangle([0, kc.H - 58, kc.CROP_W, kc.H], fill=(10, 12, 18))
    d.line([0, kc.H - 58, kc.CROP_W, kc.H - 58], fill=kc.ERR, width=2)
    d.text((kc.PAD, kc.H - 45), kc.offline_text(now - 3600), font=kc.F(kc.FB, 30), fill=kc.ERR)
    img.crop((0, 0, kc.CROP_W, kc.H)).save("/tmp/net_test.png")


np.finish = fake_finish
np.main()
print("rendered /tmp/net_test.png")
