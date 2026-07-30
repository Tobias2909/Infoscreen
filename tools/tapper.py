#!/usr/bin/env python3
"""Synthetic touch taps into the running kiosk via mpv's IPC socket.
   fast <n>  : n taps 100 ms apart on the LEFT panel (tap-storm repro; debounce should eat most)
   mixed <n> : n taps 400 ms apart alternating panel / media (every tap accepted)"""
import json, socket, sys, time

SOCK = "/tmp/mpv-kiosk.sock"
PANEL, MEDIA = (400, 500), (1700, 500)

def tap(x, y):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(3)
    s.connect(SOCK)
    s.sendall((json.dumps({"command": ["mouse", x, y, 0, "single"]}) + "\n").encode())
    try:
        s.recv(4096)
    except OSError:
        pass
    s.close()

mode = sys.argv[1]
n = int(sys.argv[2])
gap, spots = (0.10, [PANEL]) if mode == "fast" else (0.40, [PANEL, MEDIA])
for i in range(n):
    x, y = spots[i % len(spots)]
    tap(x, y)
    time.sleep(gap)
print(f"{mode}: sent {n} taps")
