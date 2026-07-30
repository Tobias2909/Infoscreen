#!/usr/bin/env python3
"""Render the shared "No internet connection" strip to banner.bgra.

Same idea as temp_badge.py: ONE bitmap the lua overlays on top of whatever screen
is active, instead of baking the strip into every screen's panel.bgra. Baked-in was
wrong for the same reason the CPU temp was wrong before it moved out: only the screen
that happens to re-render gets the current value, so during a short outage the banner
appeared on the active screen and vanished as soon as you switched screens.

This script only draws pixels. Visibility is the lua's job (net_watch -> overlay-add /
overlay-remove id 2), driven by netmon's live.json.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from PIL import Image, ImageDraw
from kiosk_common import CROP_W, PAD, ERR, FB, F, _atomic, net_online, offline_text

BH = 58                                  # strip height; lua overlays it at y = 1080-BH
DIR = os.path.dirname(os.path.abspath(__file__))

_, last_ok = net_online()                # reads netmon's live.json, does not ping
img = Image.new("RGB", (CROP_W, BH), (10, 12, 18))   # opaque: stays readable over any screen
d = ImageDraw.Draw(img)
d.line([0, 0, CROP_W, 0], fill=ERR, width=2)
d.text((PAD, 13), offline_text(last_ok), font=F(FB, 30), fill=ERR)
_atomic(DIR + "/banner.bgra", img.convert("RGBA").tobytes("raw", "BGRA"))
if os.environ.get("WK_PNG"):
    img.save(DIR + "/banner.png")
