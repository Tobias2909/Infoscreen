#!/usr/bin/env python3
"""Watchdog decision tests (netmon.wd_due) -- the part that cannot be observed on
the live screen without waiting for a real uplink outage.

Pure logic only: nothing here pings, bounces the uplink or writes netlog.
Run: python3 test_netmon.py
"""
import sys, os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import netmon as nm

T = 1_800_000_000           # arbitrary "now"
fails = []


def check(label, got, want):
    if got != want:
        fails.append("%s: got %r, want %r" % (label, got, want))


# --- 1. only the states a fresh lease can fix, and only after WD_AFTER --------
for st in ("gw_down", "wan_down", "no_lease"):
    check("%s after %ds" % (st, nm.WD_AFTER),
          nm.wd_due(st, T - nm.WD_AFTER, T, 0, 0), True)
    check("%s too fresh" % st,
          nm.wd_due(st, T - nm.WD_AFTER + 30, T, 0, 0), False)

# carrier loss is a cable, not a lease -- bouncing only adds AP downtime
check("link_down never bounces", nm.wd_due("link_down", T - 3600, T, 0, 0), False)
check("dns_down never bounces", nm.wd_due("dns_down", T - 3600, T, 0, 0), False)
check("up never bounces", nm.wd_due("up", T - 3600, T, 0, 0), False)
check("icmp_blocked never bounces",
      nm.wd_due("icmp_blocked", T - 3600, T, 0, 0), False)
check("None state (startup) never bounces",
      nm.wd_due(None, T - 3600, T, 0, 0), False)

# --- 2. rate limit + exponential backoff -------------------------------------
down = T - 7200      # long outage, so WD_AFTER is never the blocker here
check("1st retry blocked inside WD_GAP",
      nm.wd_due("gw_down", down, T, T - nm.WD_GAP + 60, 1), False)
check("1st retry allowed at WD_GAP",
      nm.wd_due("gw_down", down, T, T - nm.WD_GAP, 1), True)
# after 2 attempts the gap is 2x, after 3 it is 4x
check("2nd retry blocked at 1x gap",
      nm.wd_due("gw_down", down, T, T - nm.WD_GAP, 2), False)
check("2nd retry allowed at 2x gap",
      nm.wd_due("gw_down", down, T, T - 2 * nm.WD_GAP, 2), True)
check("3rd retry blocked at 2x gap",
      nm.wd_due("gw_down", down, T, T - 2 * nm.WD_GAP, 3), False)
check("3rd retry allowed at 4x gap",
      nm.wd_due("gw_down", down, T, T - 4 * nm.WD_GAP, 4 - 1), True)

# backoff is capped: a multi-day outage must not stretch to days between tries
check("backoff capped at WD_GAP_MAX",
      nm.wd_due("gw_down", down, T, T - nm.WD_GAP_MAX, 99), True)
check("nothing sneaks past the cap",
      nm.wd_due("gw_down", down, T, T - nm.WD_GAP_MAX + 60, 99), False)

# --- 3. first bounce of a fresh boot is not blocked by the zero stamp ---------
check("last_bounce=0 does not block", nm.wd_due("gw_down", down, T, 0, 0), True)

# --- 4. the kill switch ------------------------------------------------------
nm.WD_ENABLE = False
check("WD_ENABLE=False disables everything",
      nm.wd_due("gw_down", down, T, 0, 0), False)
nm.WD_ENABLE = True

if fails:
    print("FAIL (%d)" % len(fails))
    for f in fails:
        print("  " + f)
    sys.exit(1)
print("all watchdog decision tests passed")
