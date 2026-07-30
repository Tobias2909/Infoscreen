#!/usr/bin/env python3
"""Render a one-line alert strip to notice_<name>.bgra. Generalised from media_error.py.

This only draws pixels; the lua owns visibility (overlay-add / overlay-remove). Two callers:

  media   730x58   at (1190, 1022)  bottom of the MEDIA pane  -- a playlist entry is missing/broken
  render 1190x44   at (0, 0)        top of the LEFT panel     -- a screen's python exited non-zero

Neither collides with the offline banner, which owns (0,1022)-(1190,1080), nor with the CPU
badge at y84-148, nor with a screen title at y66.

The text comes from notice_<name>.txt rather than argv: a media filename can contain quotes and
the lua launches these through a subprocess.

  python3 notice.py <name> <width> <height>
"""
import os, sys

# No single-instance lock. kiosk_common takes one at import time keyed on the SCRIPT name, which
# would make a "media" notice and a "render" notice block each other even though they write
# different files. There is nothing to serialise: each writes its own notice_<name>.bgra through
# a PID-unique temp + atomic rename.
os.environ.setdefault("INFOSCREEN_NOLOCK", "1")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
from kiosk_common import ERR, FB, _atomic, ellip, fit_font

DIR = os.path.dirname(os.path.abspath(__file__))
MARGIN = 20

name = sys.argv[1]
W = int(sys.argv[2])
BH = int(sys.argv[3])

try:
    with open(f"{DIR}/notice_{name}.txt") as fh:
        msg = fh.read().strip()
except OSError:
    msg = ""
msg = msg or "Something went wrong"

img = Image.new("RGB", (W, BH), (10, 12, 18))   # opaque: has to stay readable over any content
d = ImageDraw.Draw(img)
d.line([0, 0, W, 0], fill=ERR, width=2)
# The NAME in the message is the whole point, so shrink before cutting: 26 -> 18 px first,
# ellipsis only if even that will not fit.
maxw = W - 2 * MARGIN
font = fit_font(d, msg, FB, 26, 18, maxw)
d.text((MARGIN, BH // 2), ellip(d, msg, font, maxw), font=font, fill=ERR, anchor="lm")

_atomic(f"{DIR}/notice_{name}.bgra", img.convert("RGBA").tobytes("raw", "BGRA"))
if os.environ.get("WK_PNG"):
    img.save(f"{DIR}/notice_{name}.png")
