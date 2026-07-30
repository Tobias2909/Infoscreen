#!/usr/bin/env python3
"""Watch the .bgra files mpv mmaps and report any moment the VISIBLE file is shorter
than the byte count mpv's overlay-add maps (offset + h*stride). A short file = SIGBUS."""
import os, sys, time

ROOT = "/home/skylab/infoscreen"
WATCH = {
    ROOT + "/dim.bgra": 1920 * 1080 * 4,
    ROOT + "/temp.bgra": 364 * 64 * 4,
}
for k in ("weather", "salmon", "cal", "news", "pihole", "releases", "deals", "net"):
    WATCH[f"{ROOT}/screens/{k}/panel.bgra"] = 1190 * 1080 * 4

dur = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
t0 = time.monotonic()
hits = []
prev = {}
samples = 0
while time.monotonic() - t0 < dur:
    samples += 1
    for p, want in WATCH.items():
        try:
            st = os.stat(p)
        except FileNotFoundError:
            hits.append((time.monotonic() - t0, p, -1, 0, want))
            continue
        key = (st.st_ino, st.st_size)
        if prev.get(p) != key:
            prev[p] = key
            if st.st_size < want:
                hits.append((time.monotonic() - t0, p, st.st_size, st.st_ino, want))
    time.sleep(0.002)

print(f"samples={samples} short_events={len(hits)}")
for t, p, size, ino, want in hits[:60]:
    print(f"  +{t:6.2f}s  {os.path.relpath(p, ROOT):28s} size={size:>9} (want {want}) ino={ino}")
