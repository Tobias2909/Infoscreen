#!/usr/bin/env python3
"""Render the live CPU temp to a tiny temp.bgra badge (top-right). The lua overlays it as id 1
(UNDER the full-screen dim id 2) and refreshes it every 15s — so the temp is ONE live source,
identical on every screen, instead of a value baked into each panel at its own render time.
Written atomically (overlay-add reads whole files -> a truncated read = SIGBUS).

Write gate: the badge only ever shows a whole degree, so re-encoding the same number was
93 KB of SD writes every 15 s = 0.54 GB/day for pixels nobody could tell apart. Same gate as
kiosk_common.write_dim() uses for dim.bgra: remember the value in temp_value.txt and skip the
write while it is unchanged. The lua's overlay-add still runs every 15 s, it just re-maps the
same file."""
import os, subprocess
from PIL import Image, ImageDraw, ImageFont

DIR = os.path.dirname(os.path.abspath(__file__))
W, H = 364, 64
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SUB = (150, 163, 180, 255)
OUT = DIR + "/temp.bgra"
STATE = DIR + "/temp_value.txt"


def cpu_temp():
    try:
        o = subprocess.check_output(["/usr/bin/vcgencmd", "measure_temp"], timeout=4).decode()
        return round(float(o.split("=")[1].split("'")[0]))
    except Exception:
        return None


t = cpu_temp()
cur = "" if t is None else str(t)
try:                                               # unchanged AND the bitmap is intact -> skip
    if open(STATE).read().strip() == cur and os.path.getsize(OUT) == W * H * 4:
        raise SystemExit(0)
except (OSError, ValueError):
    pass                                           # no/garbage state file, or badge missing

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
if t is not None:
    d.text((360, 8), f"CPU {t}°C", font=ImageFont.truetype(FR, 34), fill=SUB, anchor="ra")


def _atomic(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())        # PID-unique: a shared temp name lets two
    try:                                           # instances share one inode, and B's O_TRUNC
        with open(tmp, "wb") as f:                 # then zeroes the file A already renamed into
            f.write(data)                          # place -> mpv mmaps a short file -> SIGBUS
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_atomic(OUT, img.tobytes("raw", "BGRA"))
_atomic(STATE, (cur + "\n").encode())              # only after the bitmap really landed
