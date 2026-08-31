#!/usr/bin/env python3
"""Upcoming-releases screen — a dedicated, hand-curated countdown board for anticipated games/anime.

Source: countdowns.json (you edit it) = [{title, date, cover}]
  date : "YYYY-MM-DD" exact -> day/hour countdown
         "YYYY-MM" / "YYYY-Qn" / "YYYY" -> approximate ("~ Sep 2026")
         "TBA" (or empty) -> "To be announced", sorts last
  cover: image URL (SteamGridDB grid = 600x900 / 2:3 works perfectly). Optional; blank -> placeholder.

Layout: soonest release is FEATURED with its big cover + countdown; the rest list compactly beside it.
On release day a title shows "TODAY", then "OUT NOW" for 7 days (GRACE), then auto-hides (stays in the
json, just stops rendering — no manual cleanup). Registered in screens.conf as: releases:screens/releases/release_panel.py:120
Pure stdlib + Pillow (covers cached in icon_cache/ by kiosk_common.icon).
"""
import re, math, calendar, datetime
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, TZ, F, FB, FR, ACC, FG, SUB, SALMON, WARN,
                          CARD, LINE, icon, wrap, ellip, fit_font, new_canvas, header, finish)

CFG   = MYDIR + "/countdowns.json"
PAGEF = MYDIR + "/releases_page.txt"        # rotating page for the "Also coming" list (like news_page.txt)
TZI   = ZoneInfo(TZ)
GRACE = 7                                   # days to keep showing "OUT NOW" after release
GREEN = (120, 220, 150)


# Meteorological seasons (N. hemisphere): (end_month, end_day|None=last-of-month, year_offset).
# Winter YYYY spills into the next year, so its last day is end of Feb YYYY+1.
SEASONS = {"spring": (5, 31, 0), "summer": (8, 31, 0), "autumn": (11, 30, 0),
           "fall": (11, 30, 0), "winter": (2, None, 1)}

def _last(y, m):
    return calendar.monthrange(y, m)[1]

def classify(item, now):
    """Return sort key + status + display strings (or hidden=True). Approx dates resolve to the
    LAST day of their period (an imprecise date resolves to the latest it could be)."""
    ds = str(item.get("date", "")).strip()
    today = now.date()
    def key(d): return datetime.datetime.combine(d, datetime.time.min, TZI).timestamp()
    def approx(rd, label, big): return {"sort": key(rd), "status": "approx", "date": label, "big": big, "rd": None}

    if not ds or ds.upper() == "TBA":
        return {"sort": math.inf, "status": "tba", "date": "To be announced", "rd": None}

    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", ds)                      # exact day -> day countdown
    if m:
        rd = datetime.date(int(m[1]), int(m[2]), int(m[3]))
        delta = (rd - today).days; ds2 = rd.strftime("%d %b %Y")
        if delta < 0:
            return {"sort": key(rd), "status": "out", "date": ds2, "rd": rd} if -delta <= GRACE else {"hidden": True}
        return {"sort": key(rd), "status": "exact", "date": ds2, "rd": rd, "days": delta}

    m = re.fullmatch(r"(\d{4})-Q([1-4])", ds, re.I)                       # quarter -> last day of quarter
    if m:
        y, qq = int(m[1]), int(m[2]); em = qq * 3
        return approx(datetime.date(y, em, _last(y, em)), f"Q{qq} {y}", f"Q{qq}")

    m = re.fullmatch(r"(\d{4})-(\d{2})", ds)                             # month -> last day of month
    if m:
        y, mo = int(m[1]), int(m[2]); first = datetime.date(y, mo, 1)
        return approx(datetime.date(y, mo, _last(y, mo)), first.strftime("%b %Y"), first.strftime("%b"))

    sm = (re.fullmatch(r"(spring|summer|autumn|fall|winter)\s+(\d{4})", ds, re.I)
          or re.fullmatch(r"(\d{4})\s+(spring|summer|autumn|fall|winter)", ds, re.I))
    if sm:                                                               # season -> last day of season
        g = sm.groups()
        season = (g[0] if g[0].isalpha() else g[1]).lower()
        y = int(g[1] if g[0].isalpha() else g[0])
        em, ed, off = SEASONS[season]; ey = y + off
        return approx(datetime.date(ey, em, ed or _last(ey, em)), f"{season.capitalize()} {y}", season.capitalize())

    m = re.fullmatch(r"(\d{4})", ds)                                     # year -> last day of year
    if m:
        return approx(datetime.date(int(m[1]), 12, 31), ds, ds)

    return {"sort": math.inf, "status": "tba", "date": ds, "rd": None}   # unknown -> treat as TBA


def _load_cfg():
    import json
    try:
        with open(CFG) as f: return json.load(f)
    except Exception:
        return []


def paste_cover(img, pil, x, y, w, h):
    c = pil.convert("RGBA").resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w-1, h-1], radius=20, fill=255)
    img.paste(c, (x, y), mask)


def short_count(c):
    """Compact, fixed-width-friendly countdown badge for the list rows."""
    s = c["status"]
    if s == "exact":
        dn = c["days"]
        return f"{dn}d" if dn >= 1 else "today"
    if s == "out":    return "OUT"
    if s == "approx": return c.get("big", c["date"])       # short form: "Sep" / "Q3" / "2026"
    return "TBA"


def render():
    now = datetime.datetime.now(TZI)
    items = []
    for it in _load_cfg():
        c = classify(it, now)
        if c.get("hidden"):
            continue
        items.append((it, c))
    items.sort(key=lambda t: t[1]["sort"])

    img, d = new_canvas()
    header(d, "Upcoming")
    d.text((PANEL_W - PAD, 130), now.strftime("%a %d %b %Y"), font=F(FR, 28), fill=SUB, anchor="ra")

    if not items:
        d.text((PAD, 430), "No upcoming releases", font=F(FB, 70), fill=SUB)
        finish("releases", img, MYDIR); return

    feat_it, feat = items[0]

    # ---- featured cover (left) ----
    cx0, cy0, cw, ch = PAD, 176, 453, 680          # 2:3 box (SteamGridDB grid ratio)
    cov = icon(feat_it.get("cover")) if feat_it.get("cover") else None
    if cov:
        paste_cover(img, cov, cx0, cy0, cw, ch)
        d.rounded_rectangle([cx0, cy0, cx0+cw, cy0+ch], radius=20, outline=LINE, width=2)
    else:
        d.rounded_rectangle([cx0, cy0, cx0+cw, cy0+ch], radius=20, fill=CARD)
        tl = wrap(d, feat_it["title"], F(FB, 44), cw-60)[:4]
        for i, ln in enumerate(tl):
            d.text((cx0+cw//2, cy0+ch//2-len(tl)*30+i*60), ln, font=F(FB, 44), fill=SUB, anchor="mm")

    # ---- featured details (right of cover) — packed top-down, no dead space ----
    fx = cx0 + cw + 46
    fw = PANEL_W - PAD - fx
    if feat["status"] == "out":
        d.text((fx, 178), "JUST RELEASED", font=F(FB, 28), fill=GREEN)
    else:
        d.text((fx, 178), "NEXT UP", font=F(FB, 28), fill=ACC)
    tlines = wrap(d, feat_it["title"], F(FB, 48), fw)[:2]
    ty = 214
    for ln in tlines:
        d.text((fx, ty), ln, font=F(FB, 48), fill=FG); ty += 58
    by = ty + 14                                   # countdown sits right under the title

    st = feat["status"]
    if st == "exact":
        dn = feat["days"]
        if dn >= 3:
            big, unit, sub = str(dn), "days", "until " + feat["date"]
        else:
            secs = (datetime.datetime.combine(feat["rd"], datetime.time.min, TZI) - now).total_seconds()
            hrs = max(0, math.ceil(secs / 3600))
            big, unit, sub = (("TODAY", "", feat["date"]) if hrs <= 0
                              else (str(hrs), "hours", "until " + feat["date"]))
        d.text((fx, by), big, font=F(FB, 128), fill=FG)
        if unit:
            nw = d.textlength(big, font=F(FB, 128))
            d.text((fx+nw+18, by+62), unit, font=F(FR, 44), fill=SUB)
        sub_y = by + 150
    elif st == "out":
        d.text((fx, by), "OUT NOW", font=F(FB, 96), fill=GREEN); sub = "released " + feat["date"]; sub_y = by + 112
    elif st == "approx":
        t = "~ " + feat.get("big", feat["date"])
        d.text((fx, by), t, font=fit_font(d, t, FB, 96, 54, fw), fill=FG)
        sub = "expected " + feat["date"]; sub_y = by + 112
    else:  # tba
        d.text((fx, by), "TBA", font=F(FB, 96), fill=SUB); sub = "date not announced"; sub_y = by + 112

    d.text((fx, sub_y), sub, font=F(FR, 32), fill=SUB)
    ny = sub_y + 56

    # ---- the rest (compact list, right column) — fixed chip width so titles line up ----
    rest = items[1:]
    if rest:
        d.text((fx, ny + 12), "Also coming", font=F(FR, 28), fill=SUB)
        dv = ny + 50
        d.line([fx, dv, PANEL_W-PAD, dv], fill=LINE, width=2)
        ly = dv + 24
        list_top = ly
        PER, ROWH, CHIPW = 4, 76, 122      # show 4/page; page cycles through ALL entries across renders (like news)
        n = len(rest)
        if n > PER:
            page = 0
            try: page = int(open(PAGEF).read().strip())
            except Exception: pass
            pages = (n + PER - 1) // PER
            page %= pages
            show = rest[page*PER:(page+1)*PER]
            try: open(PAGEF, "w").write(str((page + 1) % 100000))
            except Exception: pass
        else:
            show = rest
        tx = fx + CHIPW + 22
        for it, c in show:
            chip = short_count(c)
            col = GREEN if c["status"] == "out" else (ACC if c["status"] == "exact" else SUB)
            d.rounded_rectangle([fx, ly, fx+CHIPW, ly+52], radius=14, fill=CARD)
            d.text((fx+CHIPW/2, ly+26), chip, font=F(FB, 30), fill=col, anchor="mm")
            d.text((tx, ly+2), ellip(d, it["title"], F(FB, 32), PANEL_W-PAD-tx), font=F(FB, 32), fill=FG)
            d.text((tx, ly+38), ellip(d, c["date"], F(FR, 24), PANEL_W-PAD-tx), font=F(FR, 24), fill=SUB)
            ly += ROWH
        if n > PER:                                # page indicator, centered under the 4-row block (fixed y)
            d.text(((fx + PANEL_W - PAD) / 2, list_top + PER*ROWH + 6), f"{page+1} of {pages}",
                   font=F(FR, 26), fill=SUB, anchor="ma")

    finish("releases", img, MYDIR)


render()
