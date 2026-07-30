#!/usr/bin/env python3
"""Pi-hole screen — live network/ad-block dashboard for the Pi-hole running on this box.

Pi-hole v6 REST API (the container is network_mode:host, so it answers on 127.0.0.1/api).
Auth: POST password -> session SID (cached in pihole_sid.json until it expires; re-auth on 401).
Credentials in pihole_api.json {"base","password"} (chmod 600 — the FTLCONF web password).

Draws: donut (blocked/cached/forwarded split) with big %blocked in center, queries/blocked hero,
a color-matched legend, a 24h stacked query chart, 4 device-neutral tiles (clients / blocklist /
cache-hit% / unique domains), and a blocking-status + uptime footer. Numbers climb all day, never
static. Registered in screens.conf as: pihole:screens/pihole/pihole_panel.py:310
Pure stdlib + Pillow (no venv). Data cached to pihole_cache.json -> serves stale if the API is down.
"""
import json, time, urllib.request, urllib.error
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, F, FB, FR, ACC, FG, SUB, SALMON, ERR, WARN,
                          CARD, LINE, fit_font, ellip, new_canvas, header, finish)

CFG   = MYDIR + "/pihole_api.json"
SIDF  = MYDIR + "/pihole_sid.json"
CACHE = MYDIR + "/pihole_cache.json"

GREEN = (120, 220, 150)          # cached / OK
BLOCK = (255, 95, 86)            # blocked (red)
FWD   = ACC                      # forwarded (blue)


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


def _sid(cfg, force=False):
    if not force:
        s = _load(SIDF)
        if s and s.get("expires", 0) > time.time() + 5:
            return s["sid"]
    req = urllib.request.Request(cfg["base"] + "/auth",
                                 data=json.dumps({"password": cfg["password"]}).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        sess = json.load(r).get("session", {})
    if not sess.get("valid"):
        raise RuntimeError("pihole auth invalid")
    _save(SIDF, {"sid": sess["sid"], "expires": time.time() + sess.get("validity", 1800)})
    return sess["sid"]

def _api(cfg, path):
    for attempt in (0, 1):                         # retry once with a fresh SID on 401
        sid = _sid(cfg, force=(attempt == 1))
        req = urllib.request.Request(cfg["base"] + path, headers={"sid": sid})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                continue
            raise
    return None

def fetch():
    """Return (data, status): 'ok' live, 'stale' cache after failure, 'down' no data at all."""
    cfg = _load(CFG)
    if not cfg:
        return None, "down"
    try:
        d = {
            "summary": _api(cfg, "/stats/summary"),
            "padd":    _api(cfg, "/padd"),
            "history": _api(cfg, "/history"),
            "version": _api(cfg, "/info/version"),
        }
        _save(CACHE, {"ts": time.time(), "data": d})
        return d, "ok"
    except Exception:
        c = _load(CACHE)
        if c:
            return c["data"], "stale"
        return None, "down"


def nfmt(n):
    n = int(n or 0)
    if n >= 1_000_000: return f"{n/1e6:.1f}M"
    if n >= 100_000:   return f"{n/1000:.0f}k"
    return f"{n:,}"

def upfmt(s):
    s = int(s or 0); dd, h, m = s//86400, (s % 86400)//3600, (s % 3600)//60
    if dd: return f"{dd}d {h}h"
    if h:  return f"{h}h {m}m"
    return f"{m}m"

# component key -> short label; order = display order
_VER_COMPONENTS = (("ftl", "FTL"), ("core", "Core"), ("web", "Web"), ("docker", "Docker"))

def _ver_components(data):
    """Yield (label, local, remote) per component. /info/version gives core/web/ftl as
    {local:{version},remote:{version}} and docker as {local, remote} (plain strings)."""
    v = ((data or {}).get("version") or {}).get("version") or {}
    for key, lbl in _VER_COMPONENTS:
        c = v.get(key)
        if not isinstance(c, dict):
            continue
        loc, rem = c.get("local"), c.get("remote")
        if isinstance(loc, dict): loc = loc.get("version")
        if isinstance(rem, dict): rem = rem.get("version")
        yield lbl, loc, rem

def versions(data):
    """List of (label, local_version) for every component — the INSTALLED versions, shown always."""
    return [(lbl, loc) for lbl, loc, _ in _ver_components(data) if loc]

def updates(data):
    """List of (label, remote_version) for every component whose local != remote (empty = all current)."""
    return [(lbl, rem) for lbl, loc, rem in _ver_components(data) if loc and rem and loc != rem]


def render():
    data, status = fetch()
    img, d = new_canvas()
    header(d, "Pi-hole")

    if not data or not data.get("summary"):
        d.text((PAD, 430), "Pi-hole n/a", font=F(FB, 110), fill=(200, 80, 80))
        d.text((PAD, 570), "API unreachable", font=F(FR, 34), fill=WARN)
        finish("pihole", img, MYDIR); return

    q = data["summary"]["queries"]
    total = q.get("total", 0) or 0
    blocked = q.get("blocked", 0) or 0
    cached = q.get("cached", 0) or 0
    fwd = q.get("forwarded", 0) or 0
    uniq = q.get("unique_domains", 0) or 0
    pct = q.get("percent_blocked", 0.0) or 0.0
    padd = data.get("padd") or {}
    pc = lambda v: (100.0 * v / total) if total else 0.0

    # ---- donut (blocked / cached / forwarded) with %blocked in the center ----
    cx, cy, R, TH = 218, 262, 110, 40
    bbox = [cx - R, cy - R, cx + R, cy + R]
    denom = max(1, blocked + cached + fwd)
    a0 = -90.0
    for val, col in ((cached, GREEN), (fwd, FWD), (blocked, BLOCK)):
        a1 = a0 + 360.0 * val / denom
        if val > 0:
            d.arc(bbox, a0, a1, fill=col, width=TH)
        a0 = a1
    inner_w = 2 * (R - TH) - 10                    # hole width; keep text inside so it never hits the ring
    ptxt = f"{pct:.1f}%"
    d.text((cx, cy - 14), ptxt, font=fit_font(d, ptxt, FB, 50, 28, inner_w), fill=FG, anchor="mm")
    d.text((cx, cy + 30), "blocked", font=F(FR, 24), fill=SUB, anchor="mm")

    # ---- queries total + color-matched legend (fills the space right of the donut) ----
    lx = 380
    d.text((lx, 150), "Queries today", font=F(FR, 28), fill=SUB)
    d.text((lx, 182), nfmt(total), font=F(FB, 58), fill=FG)
    ly = 268
    for name, val, col in (("Blocked", blocked, BLOCK), ("Cached", cached, GREEN),
                           ("Forwarded", fwd, FWD)):
        d.ellipse([lx, ly + 8, lx + 20, ly + 28], fill=col)
        d.text((lx + 34, ly), name, font=F(FR, 30), fill=FG)
        d.text((lx + 360, ly), nfmt(val), font=F(FB, 30), fill=FG, anchor="ra")
        d.text((lx + 470, ly), f"{pc(val):.0f}%", font=F(FR, 28), fill=SUB, anchor="ra")
        ly += 46

    # ---- 24h stacked query chart (cached/forwarded/blocked — matches the donut) — centerpiece ----
    cy0, cy1 = 452, 690                       # chart top / baseline
    hist = (data.get("history") or {}).get("history", []) if isinstance(data.get("history"), dict) else []
    d.text((PAD, 410), "Last 24 hours", font=F(FR, 28), fill=SUB)
    if hist:
        maxt = max((p.get("total", 0) for p in hist), default=1) or 1
        d.text((PANEL_W - PAD, 410), f"peak {nfmt(maxt)}/10 min", font=F(FR, 24), fill=SUB, anchor="ra")
        H = cy1 - cy0
        n = len(hist); cw2 = (PANEL_W - 2 * PAD) / n
        for i, p in enumerate(hist):
            x = PAD + i * cw2; bw = max(1, cw2 - 1); yb = cy1
            for seg, col in ((p.get("cached", 0), GREEN), (p.get("forwarded", 0), FWD),
                             (p.get("blocked", 0), BLOCK)):
                sh = H * seg / maxt
                if sh > 0:
                    d.rectangle([x, yb - sh, x + bw, yb], fill=col)
                    yb -= sh
    d.line([PAD, cy1, PANEL_W - PAD, cy1], fill=LINE, width=2)

    # ---- stat tiles (device-neutral metrics) ----
    tiles = [("Clients", nfmt(padd.get("active_clients", 0))),
             ("Blocklist", nfmt(padd.get("gravity_size", 0))),
             ("Cache hit", f"{pc(cached):.0f}%"),
             ("Domains", nfmt(uniq))]
    x0 = PAD; cw = (PANEL_W - 2 * PAD) // 4; ty = 726
    for i, (lbl, val) in enumerate(tiles):
        cxx = x0 + cw * i + cw // 2
        d.rounded_rectangle([x0 + cw*i + 8, ty, x0 + cw*(i+1) - 8, ty + 128], radius=18, fill=CARD)
        d.text((cxx, ty + 48), val, font=F(FB, 46), fill=FG, anchor="mm")
        d.text((cxx, ty + 98), lbl, font=F(FR, 26), fill=SUB, anchor="mm")

    upds = updates(data)

    # ---- footer: installed component versions (ALWAYS shown) + blocking status + uptime ----
    fy = 866
    d.line([PAD, fy, PANEL_W - PAD, fy], fill=LINE, width=2)
    vlist = versions(data)                                            # current FTL/Core/Web/Docker
    brow_y = fy + 22                                                   # blocking-status row baseline
    if vlist:                                                        # versions sit OVER "Blocking active"
        vers = "     ·     ".join(f"{n} {v}" for n, v in vlist)       # e.g. "FTL v6.7 · Core v6.4.3 · ..."
        d.text((PANEL_W // 2, fy + 8), vers,
               font=fit_font(d, vers, FR, 26, 18, PANEL_W - 2 * PAD), fill=SUB, anchor="ma")
        brow_y = fy + 58                                              # push blocking status below the versions
    enabled = (padd.get("blocking") == "enabled")
    d.ellipse([PAD, brow_y + 4, PAD + 20, brow_y + 24], fill=GREEN if enabled else WARN)
    d.text((PAD + 32, brow_y), "Blocking active" if enabled else "Blocking DISABLED",
           font=F(FB, 32), fill=FG if enabled else WARN)
    sysu = (padd.get("system") or {}).get("uptime")
    d.text((PANEL_W - PAD, brow_y), "up " + upfmt(sysu), font=F(FR, 30), fill=SUB, anchor="ra")

    # ---- update-available label (right side, just BELOW the lua's live CPU-temp badge @ y84-148;
    #      the version list itself lives in the footer above) ----
    stale_y = 160
    if upds:
        lbl = "Update available"
        lf = F(FB, 30)
        d.text((PANEL_W - PAD, 152), lbl, font=lf, fill=WARN, anchor="ra")
        dot_x = PANEL_W - PAD - d.textlength(lbl, font=lf) - 30
        d.ellipse([dot_x, 166, dot_x + 18, 184], fill=WARN)            # amber dot before the label
        stale_y = 196                                                 # stale note below the label

    if status == "stale":
        d.text((PANEL_W - PAD, stale_y), "cached — API offline", font=F(FR, 24), fill=WARN, anchor="ra")
    finish("pihole", img, MYDIR)


render()
