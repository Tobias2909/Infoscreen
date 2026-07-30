#!/usr/bin/env python3
"""Prove the auto-cycle renders the next screen BEFORE switching to it.

Invariant: at the instant panel_mode.txt flips to screen X, X's panel.bgra must have been
written moments ago -- not minutes ago. Polls panel_mode.txt every 100 ms and, on each
change, reports how old the target panel was at that instant.

  age < SWITCH_CAP (4 s)  -> render-first worked
  age = minutes           -> switched to a stale panel (cap hit, or a tap)

  python3 tools/switchorder.py [seconds]
"""
import os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 1000
MODE = ROOT + "/panel_mode.txt"


def mode():
    try:
        return open(MODE).read().strip()
    except OSError:
        return None


prev, t0 = mode(), time.time()
print("watching from %s, start screen %s" % (time.strftime("%H:%M:%S"), prev), flush=True)
while time.time() - t0 < DUR:
    time.sleep(0.1)
    cur = mode()
    if cur and cur != prev:
        now = time.time()
        p = "%s/screens/%s/panel.bgra" % (ROOT, cur)
        try:
            age = now - os.stat(p).st_mtime
        except OSError:
            age = float("nan")
        print("%s  %-9s -> %-9s  panel was %.2f s old at switch  %s" % (
            time.strftime("%H:%M:%S"), prev, cur, age,
            "RENDER-FIRST" if age < 4.5 else "stale (cap hit or tap)"), flush=True)
        prev = cur
print("done", flush=True)
