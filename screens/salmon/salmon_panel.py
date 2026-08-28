#!/usr/bin/env python3
"""Salmon Run screen — Splatoon 3 current + next coop rotation (stage, weapons, countdown),
plus a banner whenever a special event is approaching or running.

Template for a kiosk screen: fetch (cached), draw on new_canvas(), finish("salmon", img, MYDIR).
All shared plumbing lives in kiosk_common. Registered in screens.conf as:
    salmon:screens/salmon/salmon_panel.py:0   (3rd field = mid-dwell refresh seconds; 0 = only on arrival)
Data: https://splatoon3.ink/data/schedules.json -> data.coopGroupingSchedule.regularSchedules.nodes

SPECIAL EVENTS come out of the SAME document, so they cost no extra request:
    coopGroupingSchedule.bigRunSchedules.nodes       Big Run
    coopGroupingSchedule.teamContestSchedules.nodes  Eggstra Work
    currentFest                                      Splatfest
They are announced roughly a week ahead and all three lists are empty most of the time. When one
is pending the screen grows a banner under the header, drops the "next rotation" row to pay for
the space, and re-tints the whole panel so the event is obvious from across the room.

Two of those three could not be verified against live data when this was written: bigRunSchedules
was empty and currentFest was null. Everything below therefore reads through .get() and skips a
field it does not recognise, because a KeyError here would blank the screen rather than degrade it.
"""
import datetime
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, H, TZ, F, FB, FR, ACC, FG, SUB, WARN, SALMON, CARD, LINE,
                          http_json, icon, paste_center, fit_font, wrap, ellip, new_canvas, header, finish)
from PIL import Image, ImageDraw, ImageFilter
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

# ---------------------------------------------------------------- special events
# (kind, key under coopGroupingSchedule, label on the pill)
COOP_EVENTS = [("bigrun",  "bigRunSchedules",      "BIG RUN"),
               ("eggstra", "teamContestSchedules", "EGGSTRA WORK")]

def _coop_events(data, now):
    coop = ((data or {}).get("data") or {}).get("coopGroupingSchedule") or {}
    out = []
    for kind, key, label in COOP_EVENTS:
        for n in ((coop.get(key) or {}).get("nodes") or []):
            try:
                st, en = _dt(n["startTime"]), _dt(n["endTime"])
            except (KeyError, TypeError, ValueError, AttributeError):
                continue                                  # a shape we do not recognise -> ignore it
            if en <= now:
                continue                                  # already over
            s = n.get("setting") or {}
            stage = s.get("coopStage") or {}
            out.append({"kind": kind, "label": label, "start": st, "end": en,
                        "stage": stage.get("name"),
                        "weapons": [w for w in (s.get("weapons") or []) if isinstance(w, dict)][:4],
                        "title": None, "colors": []})
    return out

def _fest_event(data, now):
    """Splatfest. Not a Salmon Run mode, shown here on purpose. currentFest is null except in the
    days around a fest, and it carries no stage or weapon kit, so the banner stays text only."""
    f = ((data or {}).get("data") or {}).get("currentFest")
    if not isinstance(f, dict):
        return None
    try:
        st, en = _dt(f["startTime"]), _dt(f["endTime"])
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if en <= now:
        return None
    colors, teams = [], []
    for t in (f.get("teams") or [])[:3]:
        c = (t or {}).get("color") or {}
        try:                                              # the API gives 0..1 floats per channel
            rgb = tuple(min(255, max(0, int(round(float(c[k]) * 255)))) for k in ("r", "g", "b"))
        except (KeyError, TypeError, ValueError):
            continue
        colors.append(rgb)
        name = (t or {}).get("teamName")
        teams.append((name.strip(), rgb) if isinstance(name, str) and name.strip() else (None, rgb))
    return {"kind": "splatfest", "label": "SPLATFEST", "start": st, "end": en,
            "stage": None, "weapons": [], "title": f.get("title") or "",
            "colors": colors, "teams": teams}

def pick_event(data, now):
    """The one event worth showing: whichever is running, else whichever starts soonest."""
    evs = _coop_events(data, now)
    fest = _fest_event(data, now)
    if fest:
        evs.append(fest)
    if not evs:
        return None
    live = [e for e in evs if e["start"] <= now]
    return min(live, key=lambda e: e["end"]) if live else min(evs, key=lambda e: e["start"])

def fmt_delta(when, now):
    secs = max(0, int((when - now).total_seconds()))
    d_, h, m = secs//86400, (secs % 86400)//3600, (secs % 3600)//60
    if d_: return f"{d_}d {h}h"
    return f"{h}h {m:02d}m" if h else f"{m}m"

# ---------------------------------------------------------------- per-event lighting
# The panel is normally a near-black warm gradient. An event replaces that gradient and adds soft
# coloured glows, so the screen reads as "something is on" before any text is parsed.
TINT_TOP = {"none": (20, 16, 12), "eggstra": (78, 52, 8), "bigrun": (82, 36, 6),
            "splatfest": (26, 20, 42)}
ACCENT = {"eggstra": (255, 205, 70), "bigrun": (255, 140, 40), "splatfest": (232, 232, 244)}
FEST_FALLBACK = [(255, 70, 150), (60, 220, 205), (245, 215, 60)]

def readable(rgb, floor=150):
    """Lift a colour until it works as text on the dark card.

    Team colours are picked to look good as ink on a stage, not as small type on near black, and
    some fests ship a genuinely dark team. Scaling the channels together preserves the hue, which
    is the entire point of colouring the names."""
    r, g, b = rgb
    lum = 0.299*r + 0.587*g + 0.114*b
    if lum >= floor or lum <= 0:
        return tuple(int(v) for v in rgb)
    k = floor / lum
    return tuple(min(255, int(round(v * k))) for v in (r, g, b))

def _glows(size, spots):
    """spots = [(cx, cy, radius, rgb, opacity)]. Painted one at a time and blurred, because
    overlapping low-alpha shapes drawn in a single pass build up unpredictably."""
    layer = Image.new("RGBA", (PANEL_W, H), (0, 0, 0, 0))
    for cx, cy, r, rgb, op in spots:
        one = Image.new("RGBA", (PANEL_W, H), (0, 0, 0, 0))
        ImageDraw.Draw(one).ellipse([cx - r, cy - r, cx + r, cy + r], fill=tuple(rgb) + (255,))
        one = one.filter(ImageFilter.GaussianBlur(r * 0.55))
        one.putalpha(one.getchannel("A").point(lambda v: int(v * op)))
        layer = Image.alpha_composite(layer, one)
    full = Image.new("RGBA", size, (0, 0, 0, 0))          # clip to the panel, never the video pane
    full.paste(layer, (0, 0))
    return full

def tinted_canvas(ev):
    kind = ev["kind"] if ev else "none"
    img, d = new_canvas(top=TINT_TOP.get(kind, TINT_TOP["none"]))
    spots = []
    if kind == "eggstra":                                  # gold, poured in from the top edge
        spots = [(PANEL_W//2, -140, 660, (255, 196, 48), 0.36),
                 (250, -70, 400, (255, 228, 140), 0.20)]
    elif kind == "bigrun":                                 # heavy orange, with yellow flares
        spots = [(PANEL_W//2, -150, 720, (255, 120, 20), 0.44),
                 (205, 195, 240, (255, 214, 60), 0.22),
                 (965, 430, 205, (255, 190, 40), 0.17),
                 (630, 780, 265, (255, 150, 30), 0.13)]
    elif kind == "splatfest":                              # the three team colours, three directions
        cols = (ev.get("colors") or [])[:3] or []
        cols = cols + FEST_FALLBACK[len(cols):]
        # Order matches the order the team names are written in the banner, so the light in each
        # corner belongs to the option under it. First team top left, second rising from the
        # bottom, third top right, which mirrors the first.
        spots = [(130, -50, 520, cols[0], 0.34),
                 (PANEL_W//2, H+90, 560, cols[1], 0.30),
                 (PANEL_W-130, -50, 520, cols[2], 0.34)]
    if spots:
        img = Image.alpha_composite(img.convert("RGBA"), _glows(img.size, spots)).convert("RGB")
        d = ImageDraw.Draw(img)
    return img, d

def draw_event_banner(img, d, ev, now):
    """The strip under the header. Returns nothing; its geometry is fixed by LAYOUT_EVENT."""
    kit = bool(ev.get("weapons"))
    top, bh = 146, 200          # fixed: the body layout below must not move between event types
    x0, x1 = PAD, PANEL_W - PAD
    acc = ACCENT.get(ev["kind"], SALMON)
    d.rounded_rectangle([x0, top, x1, top + bh], radius=20, fill=CARD, outline=acc, width=3)

    lf = F(FB, 30)
    lw = d.textlength(ev["label"], font=lf)
    d.rounded_rectangle([x0 + 22, top + 18, x0 + 22 + lw + 36, top + 66], radius=24, fill=acc)
    d.text((x0 + 40 + lw/2, top + 42), ev["label"], font=lf, fill=(18, 14, 10), anchor="mm")

    live = ev["start"] <= now
    d.text((x1 - 24, top + 14), "LIVE NOW, ends in" if live else "starts in",
           font=F(FR, 24), fill=(SUB if not live else acc), anchor="ra")
    d.text((x1 - 24, top + 40), fmt_delta(ev["end"] if live else ev["start"], now),
           font=F(FB, 44), fill=FG, anchor="ra")

    span = (ev["start"].astimezone(ZoneInfo(TZ)).strftime("%a %d %b, %H:%M") + "  to  " +
            ev["end"].astimezone(ZoneInfo(TZ)).strftime("%a %d %b, %H:%M"))
    d.text((x0 + 24, top + 78), span, font=F(FR, 26), fill=SUB)

    sub = ev.get("stage") or ev.get("title") or ""
    if sub:
        d.text((x0 + 24, top + 106), ellip(d, sub, F(FR, 32), x1 - x0 - 48), font=F(FR, 32), fill=acc)

    if kit:
        cw = (x1 - x0 - 48) // 4
        for i, w in enumerate(ev["weapons"][:4]):
            slot = x0 + 24 + cw*i          # icon hard left in its slot, name beside it, so long
            paste_center(img, icon((w.get("image") or {}).get("url")), slot + 28, top + 168, 52)
            d.text((slot + 60, top + 168), ellip(d, w.get("name") or "", F(FR, 20), cw - 72),
                   font=F(FR, 20), fill=SUB, anchor="lm")   # names stop being cut off
    elif ev["kind"] == "splatfest":
        # A fest has no stage and no weapon kit, so the row spells out the three options to choose
        # between, each written in its own team colour.
        teams = [t for t in (ev.get("teams") or []) if t[0]][:3]
        if teams:
            sep, sf, avail = "   vs   ", F(FR, 26), x1 - x0 - 48
            size = 30                                     # shrink the names together until they fit
            while size > 18:
                f = F(FR, size)
                w = (sum(d.textlength(n, font=f) for n, _ in teams)
                     + d.textlength(sep, font=sf) * (len(teams) - 1))
                if w <= avail:
                    break
                size -= 2
            f, tx = F(FR, size), x0 + 24
            for i, (name, rgb) in enumerate(teams):
                if i:
                    d.text((tx, top + 172), sep, font=sf, fill=SUB, anchor="lm")
                    tx += d.textlength(sep, font=sf)
                d.text((tx, top + 172), name, font=f, fill=readable(rgb), anchor="lm")
                tx += d.textlength(name, font=f)
        else:
            # The feed gave colours but no names. Better a row of colours than an empty one.
            cols = (ev.get("colors") or [])[:3]
            cols = cols + FEST_FALLBACK[len(cols):]
            sw, gap = 150, 22
            for i, c in enumerate(cols[:3]):
                sx = x0 + 24 + i * (sw + gap)
                d.rounded_rectangle([sx, top + 154, sx + sw, top + 190], radius=12,
                                    fill=tuple(c), outline=(0, 0, 0), width=1)
            d.text((x0 + 24 + 3*(sw+gap) + 8, top + 172), "teams", font=F(FR, 22), fill=SUB, anchor="lm")

# Two layouts for the body. The plain one is byte-for-byte the pre-event screen; the event one is
# squeezed to make room for the banner, which is affordable only because the next-rotation row goes.
# The right column starts here. It used to be 700, sized around a 124px countdown; both states now
# draw the countdown at 104, which is narrower, so the column moves left and the stage image gets
# the difference. Everything on the left is checked against this: the stage name auto-shrinks to
# THX - PAD - 40, and the two label lines are comfortably shorter than that.
THX = 620

LAYOUT_PLAIN = dict(name_y=168, img_y=180, lbl_y=258, cd_y=300, cd_sz=104, until_y=448,
                    sw_y=536, card_top=588, card_bot=818, icon_y=668, icon_sz=172,
                    nm2=772, nm1=782, nxt=True)
LAYOUT_EVENT = dict(name_y=372, img_y=380, lbl_y=436, cd_y=474, cd_sz=104, until_y=606,
                    sw_y=664, card_top=740, card_bot=950, icon_y=812, icon_sz=172,
                    nm2=900, nm1=910, nxt=False)

def render():
    # Fetch splatoon3.ink at most once/hour: salmon rotations flip only every ~2 days, and the countdown
    # ticks down LOCALLY from the cached endTime (no network needed to update it). Their origin ignores
    # conditional GETs (If-None-Match/If-Modified-Since both return the full 52KB — tested), so the only
    # way to be light on them is to poll rarely. 1h = ~24 tiny fetches/day, always current within 1h of a flip.
    data, status = http_json(SCHED_URL, CACHE, max_age=3600)
    now = datetime.datetime.now(datetime.timezone.utc)
    ev = pick_event(data, now) if data else None
    img, d = tinted_canvas(ev)
    header(d, "Salmon Run")

    cur = nxt = None; when = "none"
    if data:
        cur, nxt, when = pick_nodes(data["data"]["coopGroupingSchedule"]["regularSchedules"]["nodes"])
    if not cur:
        d.text((PAD, 420), "Salmon Run n/a", font=F(FB, 110), fill=(200, 80, 80))
        d.text((PAD, 560), "splatoon3.ink unreachable", font=F(FR, 34), fill=WARN)
        finish("salmon", img, MYDIR); return

    if ev:
        draw_event_banner(img, d, ev, now)
    L = LAYOUT_EVENT if ev else LAYOUT_PLAIN

    s = cur["setting"]
    # stage thumbnail (top-right)
    stimg = icon(s["coopStage"].get("image", {}).get("url") or s["coopStage"].get("thumbnailImage", {}).get("url"))
    if stimg:
        fit = stimg.copy(); fit.thumbnail((PANEL_W-PAD-THX, 999))
        tx, ty = THX, L["img_y"]
        img.paste(fit, (tx, ty))
        d.rounded_rectangle([tx-1, ty-1, tx+fit.width, ty+fit.height], radius=10, outline=LINE, width=2)
    # stage name (auto-shrink to fit left of thumbnail)
    d.text((PAD, L["name_y"]), s["coopStage"]["name"],
           font=fit_font(d, s["coopStage"]["name"], FR, 46, 30, THX-PAD-40), fill=SALMON)
    # countdown
    ref = cur["endTime"] if when == "current" else cur["startTime"]
    d.text((PAD, L["lbl_y"]), "Current rotation ends in" if when == "current" else "Next rotation starts in",
           font=F(FR, 34), fill=SUB)
    d.text((PAD, L["cd_y"]), fmt_left(ref), font=F(FB, L["cd_sz"]), fill=FG)
    d.text((PAD, L["until_y"]), ("until " if when == "current" else "at ") + loc_lbl(ref),
           font=F(FR, 34), fill=SUB)
    # current weapons (cards)
    d.text((PAD, L["sw_y"]), "Supplied Weapons", font=F(FB, 32), fill=ACC)
    x0 = PAD; cw = (PANEL_W - 2*PAD)//4
    for i, w in enumerate(s.get("weapons", [])[:4]):
        cx = x0 + cw*i + cw//2
        d.rounded_rectangle([x0+cw*i+6, L["card_top"], x0+cw*(i+1)-6, L["card_bot"]], radius=18, fill=CARD)
        paste_center(img, icon(w.get("image", {}).get("url")), cx, L["icon_y"], L["icon_sz"])
        lines = wrap(d, w["name"], F(FR, 26), cw-30)[:2]
        oy = L["nm2"] if len(lines) == 2 else L["nm1"]
        for j, ln in enumerate(lines):
            d.text((cx, oy+j*30), ln, font=F(FR, 26), fill=FG, anchor="mm")
    # next rotation (small row) — dropped while an event is on screen, its space pays for the banner
    if L["nxt"]:
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

if __name__ == "__main__":       # guarded so test_events.py can import the parser without drawing
    render()
