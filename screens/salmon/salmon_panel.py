#!/usr/bin/env python3
"""Salmon Run screen — Splatoon 3 current + next coop rotation (stage, weapons, countdown).

Template for a kiosk screen: fetch (cached), draw on new_canvas(), finish("salmon", img, MYDIR).
All shared plumbing lives in kiosk_common. Registered in screens.conf as:
    salmon:screens/salmon/salmon_panel.py:0   (3rd field = mid-dwell refresh seconds; 0 = only on arrival)
Data: https://splatoon3.ink/data/schedules.json -> data.coopGroupingSchedule.regularSchedules.nodes
"""
import datetime
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, TZ, F, FB, FR, ACC, FG, SUB, WARN, SALMON, CARD, LINE,
                          http_json, icon, paste_center, fit_font, wrap, ellip, new_canvas, header, finish)
from zoneinfo import ZoneInfo

SCHED_URL = "https://splatoon3.ink/data/schedules.json"
CACHE = MYDIR + "/salmon_cache.json"

def _dt(s): return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
def pick_nodes(nodes):
    # (shown_node, following_node, when): current rotation if now is inside one, else next upcoming.
    now = datetime.datetime.now(datetime.timezone.utc)
    for i, n in enumerate(nodes):
        if _dt(n["startTime"]) <= now < _dt(n["endTime"]):
            return n, (nodes[i+1] if i+1 < len(nodes) else None), "current"
    fut = sorted([(_dt(n["startTime"]), i) for i, n in enumerate(nodes) if _dt(n["startTime"]) > now])
    if fut:
        i = fut[0][1]
        return nodes[i], (nodes[i+1] if i+1 < len(nodes) else None), "next"
    return None, None, "none"
def fmt_left(iso):
    secs = int((_dt(iso) - datetime.datetime.now(datetime.timezone.utc)).total_seconds())
    if secs < 0: secs = 0
    h, m = secs//3600, (secs % 3600)//60
    return f"{h}h {m:02d}m" if h >= 1 else f"{m}m"
def loc_lbl(iso):
    return _dt(iso).astimezone(ZoneInfo(TZ)).strftime("%a %d %b, %H:%M")

def render():
    # Fetch splatoon3.ink at most once/hour: salmon rotations flip only every ~2 days, and the countdown
    # ticks down LOCALLY from the cached endTime (no network needed to update it). Their origin ignores
    # conditional GETs (If-None-Match/If-Modified-Since both return the full 52KB — tested), so the only
    # way to be light on them is to poll rarely. 1h = ~24 tiny fetches/day, always current within 1h of a flip.
    data, status = http_json(SCHED_URL, CACHE, max_age=3600)
    img, d = new_canvas(top=(20, 16, 12))                      # warm salmon tint
    header(d, "Salmon Run")

    cur = nxt = None; when = "none"
    if data:
        cur, nxt, when = pick_nodes(data["data"]["coopGroupingSchedule"]["regularSchedules"]["nodes"])
    if not cur:
        d.text((PAD, 420), "Salmon Run n/a", font=F(FB, 110), fill=(200, 80, 80))
        d.text((PAD, 560), "splatoon3.ink unreachable", font=F(FR, 34), fill=WARN)
        finish("salmon", img, MYDIR); return

    s = cur["setting"]
    THX = 700   # right column start; stage name + countdown stay left of this
    # stage thumbnail (top-right)
    stimg = icon(s["coopStage"].get("image", {}).get("url") or s["coopStage"].get("thumbnailImage", {}).get("url"))
    if stimg:
        fit = stimg.copy(); fit.thumbnail((PANEL_W-PAD-THX, 999))
        tx, ty = THX, 180
        img.paste(fit, (tx, ty))
        d.rounded_rectangle([tx-1, ty-1, tx+fit.width, ty+fit.height], radius=10, outline=LINE, width=2)
    # stage name (auto-shrink to fit left of thumbnail)
    d.text((PAD, 168), s["coopStage"]["name"],
           font=fit_font(d, s["coopStage"]["name"], FR, 46, 30, THX-PAD-40), fill=SALMON)
    # countdown
    ref = cur["endTime"] if when == "current" else cur["startTime"]
    d.text((PAD, 258), "Current rotation ends in" if when == "current" else "Next rotation starts in",
           font=F(FR, 34), fill=SUB)
    d.text((PAD, 300), fmt_left(ref), font=F(FB, 124), fill=FG)
    d.text((PAD, 448), ("until " if when == "current" else "at ") + loc_lbl(ref), font=F(FR, 34), fill=SUB)
    # current weapons (cards)
    d.text((PAD, 536), "Supplied Weapons", font=F(FB, 32), fill=ACC)
    x0 = PAD; cw = (PANEL_W - 2*PAD)//4
    for i, w in enumerate(s.get("weapons", [])[:4]):
        cx = x0 + cw*i + cw//2
        d.rounded_rectangle([x0+cw*i+6, 588, x0+cw*(i+1)-6, 818], radius=18, fill=CARD)
        paste_center(img, icon(w.get("image", {}).get("url")), cx, 668, 172)
        lines = wrap(d, w["name"], F(FR, 26), cw-30)[:2]
        oy = 772 if len(lines) == 2 else 782
        for j, ln in enumerate(lines):
            d.text((cx, oy+j*30), ln, font=F(FR, 26), fill=FG, anchor="mm")
    # next rotation (small row)
    d.line([PAD, 838, PANEL_W-PAD, 838], fill=LINE, width=2)
    if nxt:
        ns = nxt["setting"]
        d.ellipse([PAD, 861, PAD+15, 876], fill=SALMON)
        hdr = "Next · " + ns["coopStage"]["name"] + "  ·  " + loc_lbl(nxt["startTime"])
        d.text((PAD+28, 868), ellip(d, hdr, F(FR, 28), PANEL_W-2*PAD-28), font=F(FR, 28), fill=SUB, anchor="lm")
        for i, w in enumerate(ns.get("weapons", [])[:4]):
            cx = x0 + cw*i + cw//2
            paste_center(img, icon(w.get("image", {}).get("url")), cx, 948, 94)
            d.text((cx, 1012), ellip(d, w["name"], F(FR, 22), cw-16), font=F(FR, 22), fill=SUB, anchor="mm")
    if status == "stale":
        d.text((PANEL_W-PAD, 1058), "cached — schedule offline", font=F(FR, 22), fill=WARN, anchor="ra")
    finish("salmon", img, MYDIR)

render()
