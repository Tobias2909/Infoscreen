#!/usr/bin/env python3
"""Attribute SD-card writes to individual infoscreen files.

Every .bgra is written with os.replace(), so each write lands on a NEW inode -- polling
(inode, mtime, size) once a second therefore counts writes exactly, and count*size is the
number of bytes that must reach the card (these files are never partially rewritten).

Runs alongside a /proc/diskstats delta, which is the device-level ground truth: this says
WHO wrote, diskstats says HOW MUCH the card actually took.

  python3 tools/writewatch.py [seconds]
"""
import glob, json, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DUR = int(sys.argv[1]) if len(sys.argv) > 1 else 660

paths = sorted(glob.glob(ROOT + "/screens/*/panel.bgra"))
paths += [ROOT + "/" + f for f in ("dim.bgra", "temp.bgra", "banner.bgra", "media_error.bgra")]
paths += [ROOT + "/screens/net/samples.jsonl", ROOT + "/screens/net/live.json",
          ROOT + "/screens/net/netlog.jsonl"]


def snap(p):
    try:
        st = os.stat(p)
        return (st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def sectors():
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if p[2] == "mmcblk0":
                return int(p[9])
    return 0


last = {p: snap(p) for p in paths}
writes = {p: 0 for p in paths}
bytes_ = {p: 0 for p in paths}
s0, t0 = sectors(), time.time()

while time.time() - t0 < DUR:
    time.sleep(1)
    for p in paths:
        cur = snap(p)
        if cur is not None and cur != last[p]:
            writes[p] += 1
            # append-only logs grow in place; .bgra files are replaced whole
            if last[p] is not None and cur[0] == last[p][0] and cur[2] > last[p][2]:
                bytes_[p] += cur[2] - last[p][2]
            else:
                bytes_[p] += cur[2]
            last[p] = cur

s1, t1 = sectors(), time.time()
el = t1 - t0
dev = (s1 - s0) * 512

print("window: %.0f s" % el)
print()
print("%-42s %7s %12s %12s" % ("file", "writes", "MB in win", "GB/day"))
attributed = 0
for p in sorted(paths, key=lambda x: -bytes_[x]):
    if writes[p] == 0:
        continue
    attributed += bytes_[p]
    print("%-42s %7d %12.2f %12.3f" % (
        p.replace(ROOT + "/", ""), writes[p], bytes_[p] / 1e6, bytes_[p] / el * 86400 / 1e9))
print()
print("attributed to infoscreen files: %.2f MB  -> %.2f GB/day" % (
    attributed / 1e6, attributed / el * 86400 / 1e9))
print("device mmcblk0 total written  : %.2f MB  -> %.2f GB/day" % (
    dev / 1e6, dev / el * 86400 / 1e9))
print(json.dumps({"window_s": el, "attributed_bytes": attributed, "device_bytes": dev}))
