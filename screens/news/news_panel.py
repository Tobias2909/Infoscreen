#!/usr/bin/env python3
"""News screen — curated RSS/Atom headlines, category-quota mix, rotating page.

Feeds live in news_feeds.json (a curated ALLOWLIST — only reputable sources appear, by
construction). Each render fills a per-category POOL from the cached feeds, then shows a
fixed quota per category (QUOTA below) = 7 headlines. A persisted page counter (news_page.txt)
advances every render, so successive renders page THROUGH the pool: same fetch, different
headlines each time (the "rotate the shown lines between the hours" behaviour). Fetch is
<=1x/hour per feed (news_cache_<i>.json); rotation is independent of fetch.

Runs under the venv (feedparser) via the re-exec guard below, so the kiosk's plain
`python3 news_panel.py` launch still gets the dep while inheriting system Pillow.
Registered in screens.conf as:  news:screens/news/news_panel.py:310
"""
import os, sys
_VENVROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "venv")
_VENV = os.path.join(_VENVROOT, "bin", "python3")
# --system-site-packages venv python3 is a symlink to system python -> detect venv via sys.prefix.
if os.path.realpath(sys.prefix) != os.path.realpath(_VENVROOT) and os.path.exists(_VENV):
    os.execv(_VENV, [_VENV] + sys.argv)

import json, time, calendar, re, urllib.request
import feedparser
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, UA, F, FB, FR, FG, SUB, ACC, SALMON, WARN,
                          wrap, ellip, new_canvas, header, finish)

CFG   = MYDIR + "/news_feeds.json"
PAGEF = MYDIR + "/news_page.txt"

# ---- mix config (tunable) ----
ORDER = ["news", "it", "ai", "games", "anime"]          # top-to-bottom display order
QUOTA = {"news": 1, "it": 3, "ai": 1, "games": 1, "anime": 1}   # slots per category = 7 total
POOL_MAX  = 15          # newest N kept per category (the rotation pool)
PER_FEED  = 20          # items pulled per feed
MAX_AGE   = 3600        # re-fetch a feed at most once/hour

CAT_COL = {"it": ACC, "ai": (178, 148, 252), "games": SALMON,
           "anime": (255, 120, 180), "news": (120, 220, 150)}
CAT_LBL = {"it": "IT", "ai": "AI", "games": "Games", "anime": "Anime", "news": "News"}


# heise & co. prefix their titles with a sub-brand + " | " (e.g. "heise+ | ...", "TechStage | ...").
# Strip that leading brand tag so only the real headline shows.
_PREFIX = re.compile(r"^\s*(heise\+?|heise online|c't|iX|Mac & i|TechStage|Telepolis)\s*\|\s*", re.I)
def clean_title(t):
    return _PREFIX.sub("", (t or "").strip()).strip()


def _load(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception:
        return None
def _save(p, o):
    try:
        with open(p, "w") as f: json.dump(o, f)
    except Exception:
        pass


def get_feed(i, feed):
    """Return (items, status). items: [{title,link,ts,src,cat}]. Cached <=1x/hour per feed."""
    cache = f"{MYDIR}/news_cache_{i}.json"
    c = _load(cache)
    if c and (time.time() - c.get("ts", 0)) < MAX_AGE:
        return c["items"], "ok"
    try:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": UA})
        raw = urllib.request.urlopen(req, timeout=25).read()
        fp = feedparser.parse(raw)
        items = []
        for e in fp.entries[:PER_FEED]:
            title = clean_title(e.get("title"))
            if not title:
                continue
            tp = e.get("published_parsed") or e.get("updated_parsed")
            ts = calendar.timegm(tp) if tp else time.time()   # feed times are UTC struct_time
            items.append({"title": title, "link": e.get("link", ""), "ts": ts,
                          "src": feed["src"], "cat": feed["cat"]})
        _save(cache, {"ts": time.time(), "items": items})
        return items, "ok"
    except Exception:
        if c:
            return c["items"], "stale"
        return [], "down"


def build_pools():
    feeds = _load(CFG) or []
    fresh_any = False
    pools = {c: [] for c in ORDER}
    for i, feed in enumerate(feeds):
        if feed.get("cat") not in pools:
            continue
        items, status = get_feed(i, feed)
        if status == "ok":
            fresh_any = True
        pools[feed["cat"]].extend(items)
    for c in pools:
        seen = set(); uniq = []
        for it in sorted(pools[c], key=lambda x: x["ts"], reverse=True):
            k = it["title"].strip().lower()
            if k in seen:
                continue
            seen.add(k); uniq.append(it)
        pools[c] = uniq[:POOL_MAX]
    return pools, fresh_any


def pick(pools, page):
    """Take QUOTA[c] items per category, windowed by page so renders page through the pool."""
    rows = []
    for c in ORDER:
        P = pools.get(c, []); n = len(P)
        if n == 0:
            continue
        q = QUOTA[c]; start = (page * q) % n
        for k in range(min(q, n)):
            rows.append(P[(start + k) % n])
    return rows


def age(ts):
    d = time.time() - ts
    if d < 90:      return "now"
    if d < 3600:    return f"{int(d/60)}m"
    if d < 86400:   return f"{int(d/3600)}h"
    return f"{int(d/86400)}d"


def render():
    pools, fresh_any = build_pools()
    page = 0
    try:
        page = int(open(PAGEF).read().strip())
    except Exception:
        pass
    rows = pick(pools, page)
    _save_txt = lambda: open(PAGEF, "w").write(str((page + 1) % 100000))
    try:
        _save_txt()
    except Exception:
        pass

    img, d = new_canvas()
    header(d, "News")

    if not rows:
        d.text((PAD, 420), "News n/a", font=F(FB, 110), fill=(200, 80, 80))
        d.text((PAD, 560), "no feeds reachable", font=F(FR, 34), fill=WARN)
        finish("news", img, MYDIR); return

    # 7 rows, each title wrapped to <=2 lines. Fonts/heights sized so all 7 fit even if every
    # title wraps (7 * ROW_H = 854 < usable height) — so we never have to show fewer headlines.
    y = 150
    ROW_H = 122
    tx = PAD + 40
    tmaxw = PANEL_W - PAD - tx
    TF = F(FB, 35); MF = F(FR, 24); LH = 42
    for it in rows:
        col = CAT_COL.get(it["cat"], SUB)
        lines = wrap(d, it["title"], TF, tmaxw)
        trunc = len(lines) > 2
        lines = lines[:2]
        if trunc:                                    # more than 2 lines -> mark the 2nd with an ellipsis
            l = lines[1]
            lines[1] = l + "…" if d.textlength(l + "…", font=TF) <= tmaxw else ellip(d, l, TF, tmaxw)
        d.ellipse([PAD, y + 9, PAD + 22, y + 31], fill=col)
        for j, ln in enumerate(lines):
            d.text((tx, y + j * LH), ln, font=TF, fill=FG)
        meta = f"{CAT_LBL.get(it['cat'], it['cat'])} · {it['src']} · {age(it['ts'])}"
        d.text((tx, y + len(lines) * LH + 2), meta, font=MF, fill=col)
        y += ROW_H

    if not fresh_any:
        d.text((PANEL_W - PAD, 128), "cached", font=F(FR, 24), fill=WARN, anchor="ra")
    finish("news", img, MYDIR)


render()
