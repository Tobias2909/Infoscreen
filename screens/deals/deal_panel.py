#!/usr/bin/env python3
"""Game-deals screen — isthereanydeal.com-style price tracker for a hand-curated watchlist.

Source: watchlist.json (you edit it) = ["Game Title", ...]
Data: IsThereAnyDeal API v2 (https://docs.isthereanydeal.com). Needs a free API key:
  register an app at https://isthereanydeal.com/apps/my/ -> put it in itad_api.json {"key": "..."} (chmod 600).
Region: country=DE -> EUR pricing.

We show the cheapest legit price across stores: third-party Steam keys (Fanatical, GMG, Humble, ...) AND
first-party direct (Steam / GOG / Epic). Each card is TAGGED with what it is (Steam key / Steam / GOG / Epic)
so key-vs-direct is obvious. Only known grey-market / account-share shops (BLOCK set) are dropped — ITAD's
default feed already excludes G2A/Kinguin, so normally nothing is filtered.

SECOND SOURCE — CDKeys (2026-07-26): ITAD dropped keyshops entirely, so CDKeys is invisible to it. CDKeys
renamed itself "Loaded" (cdkeys.com -> loaded.com) and is tracked by AllKeyShop as merchant id 9. We query
AllKeyShop's extension endpoint for THAT MERCHANT ONLY and draw its price as a second line on the card:
  https://www.allkeyshop.com/api/v2-1-250304/vakrs_extension.php?action=CatalogV2&...&offers.merchant.id:or=9
No key/registration. One request covers the whole watchlist (id:or=<csv>), ~900 bytes, refetched at most
once per 6 h (AKS_MAX_AGE) with an hourly retry backoff after a failure. Titles are resolved to AllKeyShop
product ids ONCE and pinned in aks_ids.json — the search is fuzzy (it happily returns "Persona 4 Golden"
for "Persona 4 Revival"), so same_game() guards every resolve and stores id=null rather than a wrong pin.
If the endpoint stops answering (blocked, or they change the API), the screen says so instead of silently
dropping the line — see aks_note(). Only in-stock, non-account offers count; Standard edition preferred.

Per render (ITAD data cached ~1h, CDKeys ~6h): resolve each title -> ITAD game UUID + boxart (cached in
itad_ids.json), then POST the UUIDs to games/prices/v3 -> deals, drop blocked shops, take cheapest. Shows a
cover-art card per game with store, tag, discount %, price, all-time-low context and the CDKeys price.
Long lists page across renders. Registered in screens.conf as: deals:screens/deals/deal_panel.py:120.
Pure stdlib + Pillow.
"""
import json, time, datetime, re, difflib, urllib.request, urllib.parse, urllib.error
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root
MYDIR = os.path.dirname(os.path.abspath(__file__))
from kiosk_common import (PAD, PANEL_W, H, TZ, UA, F, FB, FR, FL, ACC, FG, SUB, SALMON, WARN,
                          CARD, LINE, icon, ellip, wrap, new_canvas, header, finish)

WATCH   = MYDIR + "/watchlist.json"
IDS     = MYDIR + "/itad_ids.json"       # title -> {"id":uuid|null, "box":url}   (null id = confirmed not-found)
CACHE   = MYDIR + "/deal_cache.json"     # {ts, data:{prices:{uuid:entry}}}
KEYF    = MYDIR + "/itad_api.json"       # {"key": "..."} chmod 600
PAGEF   = MYDIR + "/deals_page.txt"
AKS_IDS   = MYDIR + "/aks_ids.json"      # title -> {"id":int|null, "name":str}   (null id = no confident match)
AKS_CACHE = MYDIR + "/aks_cache.json"    # {ts, last_ok, next_try, err, data:{title:{price,region,edition}}}
TZI     = ZoneInfo(TZ)

MAX_AGE = 3600                           # refetch ITAD prices at most once/hour
GREEN   = (120, 220, 150)
KEY_COL = (176, 148, 252)                # CDKeys line — violet, deliberately not a first-party store colour
CHIPBG  = (40, 47, 62)
API     = "https://api.isthereanydeal.com"
COUNTRY = "DE"
SYM     = {"EUR": "€", "USD": "$", "GBP": "£"}
PER, CARDH, GAP, ROW_TOP = 4, 190, 16, 196
# grey-market / account-share shops to drop (ITAD's default feed usually excludes these anyway)
BLOCK   = ("g2a", "kinguin", "eneba", "gamivo", "hrkgame", "difmark", "k4g", "igvault")

# --- AllKeyShop / CDKeys ---
AKS_API      = "https://www.allkeyshop.com/api/v2-1-250304/vakrs_extension.php"
AKS_MERCHANT = 9                         # "Loaded" = CDKeys after the rename
AKS_MAX_AGE  = 6 * 3600                  # refetch CDKeys prices at most once/6h
AKS_RETRY    = 3600                      # after a failure, don't retry before this (renders are frequent)
AKS_QUIET    = 2 * AKS_MAX_AGE           # only warn on screen once cached prices are this old
AKS_FIELDS   = "id,name,offers.price,offers.region.name,offers.edition.name,offers.stock_status"
AKS_BASE     = {"action": "CatalogV2", "sort_field": "relevance", "sort_order": "desc", "pagenum": 1,
                "type": "game", "locale": "en", "price_mode": "price", "currency": "EUR",
                "operating_systems": "pc"}


# ---------- data ----------
def _load(p, default=None):
    try:
        with open(p) as f: return json.load(f)
    except Exception:
        return default

def _save(p, o):
    try:
        with open(p, "w") as f: json.dump(o, f)
    except Exception:
        pass

def _api(path, params, body=None):
    url = f"{API}{path}?" + urllib.parse.urlencode(params)
    hdr = {"ITAD-API-Key": _KEY, "User-Agent": UA}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        hdr["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdr, method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)

def resolve_ids(titles):
    """Fill itad_ids.json with title->{id, box}. Retries titles that errored / were never resolved;
    stores id=null for confirmed not-found so search isn't hammered for them."""
    ids = _load(IDS, {}) or {}
    changed = False
    for t in titles:
        cur = ids.get(t)
        if isinstance(cur, dict):                       # already resolved (uuid OR confirmed null)
            continue
        try:
            res = _api("/games/search/v1", {"title": t, "results": 1})
            if res:
                ids[t] = {"id": res[0]["id"], "box": (res[0].get("assets") or {}).get("boxart")}
            else:
                ids[t] = {"id": None, "box": None}
            changed = True
        except Exception:
            pass                                        # leave unresolved -> retry next fetch
    if changed:
        _save(IDS, ids)
    return ids

def get_data(titles):
    """Return (ids, prices, status). status: 'ok' | 'stale' | 'nokey' | 'down'."""
    ids = _load(IDS, {}) or {}
    cache = _load(CACHE)
    if cache and (time.time() - cache.get("ts", 0)) < MAX_AGE:
        return ids, cache["data"].get("prices", {}), "ok"
    if not _KEY:
        return ids, (cache["data"].get("prices", {}) if cache else {}), ("stale" if cache else "nokey")
    try:
        ids = resolve_ids(titles)
        uuids = [v["id"] for v in ids.values() if isinstance(v, dict) and v.get("id")]
        prices = {}
        if uuids:
            res = _api("/games/prices/v3", {"country": COUNTRY, "nondeals": "true"}, body=uuids)
            prices = {e["id"]: e for e in res}
        _save(CACHE, {"ts": time.time(), "data": {"prices": prices}})
        return ids, prices, "ok"
    except Exception:
        return ids, (cache["data"].get("prices", {}) if cache else {}), ("stale" if cache else "down")


# ---------- CDKeys (AllKeyShop, merchant 9 = "Loaded") ----------
class AksError(Exception):
    pass

def _aks(params):
    url = AKS_API + "?" + urllib.parse.urlencode({**AKS_BASE, **params}, safe=":")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.load(r)
    except urllib.error.HTTPError as e:
        raise AksError("HTTP %d" % e.code)
    except urllib.error.URLError:
        raise AksError("no connection")
    except ValueError:
        raise AksError("bad response")
    if not isinstance(res, dict) or "products" not in res:
        raise AksError("unexpected format")             # they changed the API -> say so, don't guess
    return res

_ROMAN = ((" ii", " 2"), (" iii", " 3"), (" iv", " 4"), (" vi", " 6"), (" v", " 5"))

def _norm(s):
    s = re.sub(r"[^a-z0-9 ]+", " ", (s or "").lower())
    s = re.sub(r"\s+", " ", s).strip()
    for r, a in _ROMAN:
        if s.endswith(r):
            return s[:-len(r)] + a
    return s

def _tail(s):
    """Trailing sequel marker ('2', '3rd', ...). Sequels differ ONLY here and a fuzzy ratio can't tell
    'Trails in the Sky' from 'Trails in the Sky the 3rd', so this must match exactly."""
    m = re.search(r"\b(\d+(?:st|nd|rd|th)?)$", s)
    return m.group(1) if m else ""

def same_game(mine, theirs):
    a, b = _norm(mine), _norm(theirs)
    if _tail(a) != _tail(b):
        return False
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.9

def aks_resolve(titles):
    """title -> {"id":int|null, "name":str}. Pinned forever; null = no confident match (search is fuzzy,
    so a wrong pin would silently show the wrong game's price for good)."""
    ids = _load(AKS_IDS, {}) or {}
    changed = False
    for t in titles:
        if isinstance(ids.get(t), dict):
            continue
        res = _aks({"per_page": 3, "fields": "id,name", "search_name": t})
        hit = next((p for p in res["products"] if same_game(t, p.get("name"))), None)
        ids[t] = {"id": hit["id"], "name": hit["name"]} if hit else {"id": None, "name": None}
        changed = True
    if changed:
        _save(AKS_IDS, ids)
    return ids

def _region(name):
    n = (name or "").replace("Steam", "").strip()
    return n.upper() if n else "GLOBAL"

def _pick(offers):
    """Cheapest in-stock KEY offer; Standard edition wins over Deluxe/Ultimate at any price."""
    usable = [o for o in offers
              if o.get("stock_status") in (None, "in_stock")
              and "account" not in ((o.get("region") or {}).get("name", "")).lower()
              and o.get("price") is not None]
    if not usable:
        return None
    std = [o for o in usable if ((o.get("edition") or {}).get("name") or "") == "Standard"]
    o = min(std or usable, key=lambda x: x["price"])
    return {"price": o["price"], "region": _region((o.get("region") or {}).get("name")),
            "edition": (o.get("edition") or {}).get("name") or ""}

def aks_fetch(titles):
    ids = aks_resolve(titles)
    want = {v["id"]: t for t, v in ids.items() if isinstance(v, dict) and v.get("id")}
    if not want:
        return {}
    res = _aks({"per_page": max(20, len(want)), "fields": AKS_FIELDS,
                "offers.merchant.id:or": AKS_MERCHANT,
                "id:or": ",".join(str(i) for i in want)})
    out = {}
    for p in res["products"]:
        t = want.get(p.get("id"))
        best = _pick(p.get("offers") or [])
        if t and best:
            out[t] = best                               # absent title = CDKeys simply has no offer
    return out

def aks_get(titles):
    """Return (data, note). note is a user-facing warning string when the endpoint has stopped working."""
    c = _load(AKS_CACHE, {}) or {}
    now = time.time()
    if (now - c.get("ts", 0)) >= AKS_MAX_AGE and now >= c.get("next_try", 0):
        try:
            c = {"ts": now, "last_ok": now, "next_try": 0, "err": None, "data": aks_fetch(titles)}
        except AksError as e:
            c["err"], c["next_try"] = str(e), now + AKS_RETRY
        except Exception as e:
            c["err"], c["next_try"] = type(e).__name__, now + AKS_RETRY
        _save(AKS_CACHE, c)
    return (c.get("data") or {}), aks_note(c, now)

def aks_note(c, now):
    """Blips stay silent (cached prices are still shown); a lasting failure — blocked, or the API changed —
    gets said out loud, because a missing CDKeys line otherwise looks like 'no offer'."""
    if not c.get("err"):
        return None
    last_ok = c.get("last_ok", 0)
    if last_ok and (now - last_ok) < AKS_QUIET:
        return None
    when = datetime.datetime.fromtimestamp(last_ok, TZI).strftime("%a %d %b %H:%M") if last_ok else "never"
    return f"CDKeys prices paused — AllKeyShop API: {c['err']} · last ok {when}"


# ---------- view model ----------
def money(m):
    return (SYM.get(m.get("currency"), "") + f"{m['amount']:.2f}") if m else None

def blocked(deal):
    n = (deal.get("shop") or {}).get("name", "").lower()
    return any(b in n for b in BLOCK)

def kind(deal):
    """What you actually get, for the card tag."""
    shop = (deal.get("shop") or {}).get("name", "")
    drm = [x.get("name") for x in (deal.get("drm") or [])]
    if "Steam" in drm:      return "Steam" if shop == "Steam" else "Steam key"
    if shop == "Steam":     return "Steam"
    if "Drm Free" in drm or shop == "GOG": return "GOG" if shop == "GOG" else "DRM-free"
    if "Epic" in drm or "Epic" in shop:    return "Epic"
    return drm[0] if drm else "key"

def view(title, ids, prices, cdk):
    ent = ids.get(title)
    box = ent.get("box") if isinstance(ent, dict) else None
    base = {"title": title, "cdk": cdk.get(title)}
    if not isinstance(ent, dict):
        return {**base, "box": None, "state": "pending"}
    if ent.get("id") is None:
        return {**base, "box": None, "state": "notfound"}
    g = prices.get(ent["id"])
    all_deals = (g or {}).get("deals") or []
    deals = [d for d in all_deals if not blocked(d)]
    hlow = (((g or {}).get("historyLow") or {}).get("all"))
    if not deals:
        return {**base, "box": box, "state": "nodeal",
                "atl": money(hlow), "had": bool(all_deals)}
    best = min(deals, key=lambda x: x["price"]["amount"])
    price = best["price"]["amount"]
    atl_amt = hlow["amount"] if hlow else None
    is_atl = atl_amt is not None and price <= atl_amt + 0.01     # trust price vs ATL, not ITAD's flag
    return {**base, "box": box, "state": "deal",
            "price": money(best["price"]), "regular": money(best.get("regular")),
            "cut": best.get("cut") or 0, "shop": (best.get("shop") or {}).get("name", ""),
            "kind": kind(best), "atl": money(hlow), "is_atl": is_atl}


# ---------- drawing helpers ----------
def cover(img, pil, x, y, w, h, r=12):
    """Center-crop-fill `pil` into a w×h rounded box."""
    src = pil.convert("RGBA")
    sw, sh = src.size
    s = max(w / sw, h / sh)
    src = src.resize((max(1, int(sw * s)), max(1, int(sh * s))))
    l, t = (src.width - w) // 2, (src.height - h) // 2
    src = src.crop((l, t, l + w, t + h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    img.paste(src, (x, y), mask)

def chip(d, x, y, text, fg):
    w = d.textlength(text, font=F(FR, 26)) + 32
    d.rounded_rectangle([x, y, x + w, y + 42], radius=13, fill=CHIPBG)
    d.text((x + 16, y + 8), text, font=F(FR, 26), fill=fg)
    return w


# ---------- render ----------
def draw_setup_screen(d):
    d.text((PAD, 300), "Game deals", font=F(FB, 64), fill=FG)
    for i, ln in enumerate([
        "Set up the price tracker:",
        "1. Register an app at isthereanydeal.com/apps/my/",
        "2. Put the API key in screens/deals/itad_api.json",
        '     {"key": "..."}   (chmod 600)',
        "3. List games in screens/deals/watchlist.json"]):
        d.text((PAD, 420 + i * 58), ln, font=F(FR if i else FB, 34), fill=SUB if i else WARN)


def draw_card(img, d, cy, v):
    x0, x1 = PAD, PANEL_W - PAD
    st = v["state"]
    accent = GREEN if (st == "deal" and v["is_atl"]) else (SALMON if st == "deal" else LINE)

    d.rounded_rectangle([x0, cy, x1, cy + CARDH], radius=18, fill=CARD)
    d.rounded_rectangle([x0 + 8, cy + 12, x0 + 16, cy + CARDH - 12], radius=4, fill=accent)

    # cover art
    tw, th = 110, CARDH - 36
    tx0 = x0 + 32
    cov = icon(v["box"]) if v.get("box") else None
    if cov:
        cover(img, cov, tx0, cy + 18, tw, th)
    else:
        d.rounded_rectangle([tx0, cy + 18, tx0 + tw, cy + 18 + th], radius=12, fill=CHIPBG)
    tx = tx0 + tw + 30
    textw = x1 - 250 - tx - 16                     # reserve a 250px price zone on the right

    # title: up to 2 lines (long JRPG titles need it)
    lines = wrap(d, v["title"], F(FB, 36), textw)[:2]
    if len(lines) == 2:
        lines[1] = ellip(d, lines[1], F(FB, 36), textw)
    ty = cy + 20
    for ln in lines:
        d.text((tx, ty), ln, font=F(FB, 36), fill=FG); ty += 42

    # chip + info: right under a 1-line title; but on a cramped 2-line title, pin higher + tighter
    # so the "lowest ever" line isn't jammed against the card's bottom edge (single-line cards unchanged)
    one = len(lines) == 1
    meta_y = (ty + 6) if one else (cy + 100)
    info_dy = 56 if one else 50

    rx = x1 - 30
    if st == "deal":
        cw = chip(d, tx, meta_y, v["shop"], ACC)
        d.text((tx + cw + 14, meta_y + 8), v["kind"], font=F(FR, 24),
               fill=ACC if v["kind"].endswith("key") else SUB)
        info = []
        if v["cut"] and v["regular"]: info.append("was " + v["regular"])
        if v["atl"]:                  info.append("lowest ever " + v["atl"])
        if info:
            d.text((tx, meta_y + info_dy), ellip(d, "   ·   ".join(info), F(FR, 24), textw),
                   font=F(FR, 24), fill=GREEN if v["is_atl"] else SUB)
        d.text((rx, cy + 34), v["price"], font=F(FB, 56), fill=GREEN if v["is_atl"] else FG, anchor="ra")
        if v["cut"]:
            pill = f"-{v['cut']}%"
            pw = d.textlength(pill, font=F(FB, 28)) + 26
            d.rounded_rectangle([rx - pw, cy + 100, rx, cy + 142], radius=13, fill=CHIPBG)
            d.text((rx - pw / 2, cy + 121), pill, font=F(FB, 28),
                   fill=GREEN if v["is_atl"] else SALMON, anchor="mm")
    else:
        msg = {"nodeal": "only grey-market listings" if v.get("had") else "no price yet",
               "notfound": "not found on IsThereAnyDeal", "pending": "looking up…"}[st]
        d.text((tx, meta_y), msg, font=F(FR, 28), fill=SUB)
        if st == "nodeal" and v.get("atl"):
            d.text((tx, meta_y + 44), "lowest ever  " + v["atl"], font=F(FR, 24), fill=SUB)
        d.text((rx, cy + 60), "—" if st != "pending" else "…", font=F(FL, 60), fill=SUB, anchor="ra")

    # CDKeys, bottom of the reserved price zone (keyshop key — not the same product as a store purchase)
    k = v.get("cdk")
    if k:
        ed = "" if k["edition"] in ("", "Standard") else " · " + k["edition"]
        rg = "" if k["region"] == "GLOBAL" else " · " + k["region"]   # only flag RESTRICTED keys
        txt = f"CDKeys €{k['price']:.2f}{rg}{ed}"
        d.text((rx, cy + 152), ellip(d, txt, F(FR, 24), 244), font=F(FR, 24), fill=KEY_COL, anchor="ra")


def render():
    global _KEY
    _KEY = (_load(KEYF, {}) or {}).get("key")
    titles = _load(WATCH, []) or []
    now = datetime.datetime.now(TZI)

    img, d = new_canvas()
    header(d, "Deals")
    d.text((PAD, 138), "best store price  ·  ", font=F(FR, 28), fill=SUB)
    d.text((PAD + d.textlength("best store price  ·  ", font=F(FR, 28)), 138),
           "CDKeys keyshop", font=F(FR, 28), fill=KEY_COL)
    d.text((PANEL_W - PAD, 130), now.strftime("%a %d %b %Y"), font=F(FR, 28), fill=SUB, anchor="ra")

    if not _KEY and not _load(CACHE):
        draw_setup_screen(d); finish("deals", img, MYDIR); return
    if not titles:
        d.text((PAD, 430), "Watchlist is empty", font=F(FB, 60), fill=SUB)
        finish("deals", img, MYDIR); return

    ids, prices, _status = get_data(titles)
    cdk, cdk_note = aks_get(titles)
    views = [view(t, ids, prices, cdk) for t in titles]

    if cdk_note:
        d.text((PAD, 168), ellip(d, cdk_note, F(FR, 22), PANEL_W - 2 * PAD), font=F(FR, 22), fill=WARN)

    n = len(views)
    page, pages = 0, (n + PER - 1) // PER
    if n > PER:
        try: page = int(open(PAGEF).read().strip())
        except Exception: pass
        page %= pages
        try: open(PAGEF, "w").write(str((page + 1) % 100000))
        except Exception: pass
    show = views[page * PER:(page + 1) * PER]

    cy = ROW_TOP
    for v in show:
        draw_card(img, d, cy, v)
        cy += CARDH + GAP

    d.text((PAD, H - 46), "prices via IsThereAnyDeal.com · CDKeys via AllKeyShop.com",
           font=F(FR, 22), fill=SUB)
    if pages > 1:
        d.text((PANEL_W - PAD, H - 46), f"{page + 1} of {pages}", font=F(FR, 24), fill=SUB, anchor="ra")

    finish("deals", img, MYDIR)


render()
