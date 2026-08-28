#!/usr/bin/env python3
"""Composite one screen into an ordinary PNG, stacked the way mpv actually stacks it.

mpv IS the display here: it plays the media in the right ~38% and paints the panel, the live
CPU-temp badge and the dim as GPU overlays on top of it (overlay ids 0 / 1 / 2 -- see kiosk.lua).
Nothing on this labwc session can grab that framebuffer, so this rebuilds the same stack from the
very files mpv reads:

    background #0c1018  <  media (aspect-fit, right 38%)  <  panel.bgra  <  temp.bgra  <  dim.bgra

Handy for "what does the screen look like right now" over ssh, and it is how the README
screenshots are produced -- there, from a throwaway tree filled with example data, because a
grab of the live screen would show a real calendar and a real watchlist.

Usage:  tools/screenshot.py <key> <out.png> [--media FILE] [--width N] [--no-dim] [--root DIR]
        <key>       screen key as used in screens.conf (weather, salmon, cal, ...)
        --media     image to place in the video pane (default: none, pane stays background)
        --width     scale the 1920x1080 result down to this width (default: no scaling)
        --no-dim    skip the dim layer. The dim exists so the display is bearable in a dark
                    room; in a picture it only makes the screen look murky, so the README
                    screenshots are taken without it.
        --root      project root (default: the parent of this script's directory)
"""
import argparse, os, sys
from PIL import Image

W, H = 1920, 1080
PANEL_W_BGRA = 1190              # what overlay-add maps for id 0 (stride 4760)
TEMP_W, TEMP_H = 364, 64         # id 1
TEMP_X, TEMP_Y = 730, 84         # its position, from kiosk.lua
VIDEO_X = int(0.62 * W)          # --video-margin-ratio-left=0.62 -> video starts here
BG = (12, 16, 24)                # mpv --background="#0c1018"


def bgra(path, size):
    """Load a raw BGRA overlay file the way overlay-add maps it, or None if it is not usable."""
    need = size[0] * size[1] * 4
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < need:                  # short file: mpv would SIGBUS, we just skip it
        return None
    return Image.frombytes("RGBA", size, data[:need], "raw", "BGRA")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("key")
    ap.add_argument("out")
    ap.add_argument("--media")
    ap.add_argument("--width", type=int)
    ap.add_argument("--no-dim", action="store_true")
    ap.add_argument("--root")
    a = ap.parse_args()

    root = a.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    frame = Image.new("RGBA", (W, H), BG + (255,))

    # ---- the video pane: mpv aspect-fits the media into the region right of VIDEO_X ----
    if a.media:
        vw, vh = W - VIDEO_X, H
        m = Image.open(a.media).convert("RGBA")
        scale = min(vw / m.width, vh / m.height)
        m = m.resize((max(1, round(m.width * scale)), max(1, round(m.height * scale))),
                     Image.LANCZOS)
        frame.alpha_composite(m, (VIDEO_X + (vw - m.width) // 2, (vh - m.height) // 2))

    # ---- the overlays, in mpv's own id order ----
    panel = bgra(f"{root}/screens/{a.key}/panel.bgra", (PANEL_W_BGRA, H))
    if panel is None:
        sys.exit(f"screenshot: no usable panel.bgra for screen '{a.key}' -- render it first")
    frame.alpha_composite(panel, (0, 0))

    temp = bgra(f"{root}/temp.bgra", (TEMP_W, TEMP_H))
    if temp is not None:
        frame.alpha_composite(temp, (TEMP_X, TEMP_Y))

    if not a.no_dim:
        dim = bgra(f"{root}/dim.bgra", (W, H))
        if dim is not None:
            frame.alpha_composite(dim, (0, 0))

    out = frame.convert("RGB")
    if a.width and a.width != W:
        out = out.resize((a.width, round(H * a.width / W)), Image.LANCZOS)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    out.save(a.out, optimize=True)
    print(f"{a.key} -> {a.out} ({out.width}x{out.height})")


if __name__ == "__main__":
    main()
