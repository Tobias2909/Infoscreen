#!/usr/bin/env python3
"""Regression test: a media file mpv cannot load must NEVER end the mpv process.

That was the crash. playlist.txt line "marine_sitting" was missing its .mp4, so roughly one
media change in 46 hit it, loadfile failed, the playlist ran empty and mpv exited with
"Exiting... (Some errors happened)". kiosk.sh respawned it -> the display flashed. It looked
like "crashes after hours, when I switch screens" because the 35-min auto-rotate rolls the
dice slowly while a burst of taps rolls it fast.

Invariant checked here: mpv's PID does not change while bad loads are forced at it.

This deliberately BYPASSES the lua's own missing-entry filter by driving loadfile straight
over the IPC socket -- a fix has to be verified against the worst case it is supposed to
survive, not against the guard that normally hides it.

  python3 tools/badmedia_test.py [count] [interval_s]
"""
import json, os, socket, subprocess, sys, threading, time

SOCK = "/tmp/mpv-kiosk.sock"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COUNT = int(sys.argv[1]) if len(sys.argv) > 1 else 20
GAP = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5


def mpv_pids():
    try:
        out = subprocess.run(["pgrep", "-x", "mpv"], capture_output=True, text=True).stdout
        return sorted(int(p) for p in out.split())
    except Exception:
        return []


start = mpv_pids()
if len(start) != 1:
    sys.exit("expected exactly one mpv, found %r" % start)
pid = start[0]
print("mpv pid %d" % pid)

stop = threading.Event()
samples, changes = [0], []


def poll():
    while not stop.is_set():
        now = mpv_pids()
        samples[0] += 1
        if now != start:
            changes.append((time.strftime("%H:%M:%S"), now))
        time.sleep(0.1)


t = threading.Thread(target=poll, daemon=True)
t.start()

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(SOCK)
for i in range(COUNT):
    bad = "%s/media/__missing_%02d__.mp4" % (ROOT, i)
    s.sendall((json.dumps({"command": ["loadfile", bad]}) + "\n").encode())
    time.sleep(GAP)
s.close()

time.sleep(3)
stop.set()
t.join()

try:
    with open(ROOT + "/media_error.txt") as f:
        note = f.read().strip()
except OSError:
    note = "(no media_error.txt)"

print("forced %d bad loads, %d pid samples" % (COUNT, samples[0]))
print("on-screen notice: %s" % note)
if changes:
    print("FAIL: mpv pid changed -> %r" % changes[:5])
    sys.exit(1)
print("PASS: mpv pid stable at %d across the whole run" % pid)
