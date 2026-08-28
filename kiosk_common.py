#!/usr/bin/env python3
"""Shared toolkit for kiosk screens (weather, salmon, ...).

A "screen" is a Python script under screens/<key>/ that renders the LEFT panel and writes
screens/<key>/panel.bgra (via finish(key,img,MYDIR)) + the shared root dim.bgra. mpv overlays
that file for whichever screen is active; the lua switches screens on a tap of the left panel.

ADDING A SCREEN (see screens/salmon/ as the template):
  1. mkdir screens/<key>; copy the template. Its header must put root on sys.path + set MYDIR:
       sys.path.insert(0, <root>); MYDIR = os.path.dirname(os.path.abspath(__file__))
       img,d = new_canvas(); header(d,"My Title"); ...draw... ; finish("<key>", img, MYDIR)
  2. Register in screens.conf:  <key>:screens/<key>/<script>.py:<seconds>   (seconds 0 = no mid-dwell refresh)
  3. Restart the kiosk. Cycle order = order in screens.conf.

Everything here is stdlib + Pillow only. Keep per-render cost low: this can run once/minute.
"""
import json, datetime, os, sys, time, hashlib, math, subprocess, fcntl, signal
from zoneinfo import ZoneInfo
from PIL import Image, ImageDraw, ImageFont

# ---- one render at a time per script (import-time side effect, on purpose) ----
# A tap storm used to stack several instances of the SAME screen script (tap-arrival render +
# the per-screen interval refresh + the 600 s sweep + kiosk.sh's boot burst). Two concurrent
# writers of one .bgra is exactly what produced the "Bus error" crashes -- see _atomic().
# This lock is the upstream half of that fix: the second instance exits instead of racing.
# INFOSCREEN_NOLOCK=1 opts out (only for debugging two renders side by side).
_LOCK_FD = None
RENDER_DEADLINE = 600      # s; must exceed the slowest honest render (news: 12 feeds x 25 s timeout)
def _single_instance():
    global _LOCK_FD
    if os.environ.get("INFOSCREEN_NOLOCK"):
        return
    tag = os.path.basename(sys.argv[0]) or "kiosk"
    _LOCK_FD = open(f"/tmp/infoscreen-{tag}.lock", "w")
    try:
        fcntl.flock(_LOCK_FD, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"{tag}: another instance is already rendering, exiting", file=sys.stderr)
        sys.exit(0)
    # A render must never outlive its lock, or one wedged fetch would freeze that screen forever
    # (the old behaviour just piled more instances on). SystemExit unwinds through _atomic's
    # cleanup, so no partial temp file is left behind.
    def _deadline(sig, frame):
        raise SystemExit(f"{tag}: render exceeded {RENDER_DEADLINE}s, aborting")
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(RENDER_DEADLINE)
_single_instance()

# ---- geometry ----
W, H = 1920, 1080          # full screen
PANEL_W = 1180             # gradient panel width (left)
CROP_W = 1190              # BGRA overlay width mpv expects (stride 4760)
PAD = 90                   # standard left/right content margin
DIR = os.path.dirname(os.path.abspath(__file__))

# ---- location / net identity ----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # so `import sundim` works
import sundim                                                     # shared sun/dim maths,
from sundim import (LAT, LON, TZ, LABEL, UA,                       # location + contact come
                    solar_brightness, dim_alpha, DIM_STEP)        # from machine-local config
ICON_CACHE = DIR + "/icon_cache"

# ---- fonts (memoized: truetype reload per call is wasteful at 60s cadence) ----
FB = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FL = "/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf"
_FC = {}
def F(path, size):
    k = (path, size); f = _FC.get(k)
    if f is None: f = ImageFont.truetype(path, size); _FC[k] = f
    return f

# ---- palette ----
ACC=(127,209,255); FG=(238,242,248); SUB=(150,163,180); CLOUD=(176,186,200)
WARN=(255,180,70); ERR=(255,70,70); SALMON=(255,140,50); CARD=(26,31,42); LINE=(42,48,60)

# ---- system ----
def pitemp():
    try:
        o = subprocess.check_output(["/usr/bin/vcgencmd","measure_temp"], timeout=4).decode()
        return round(float(o.split("=")[1].split("'")[0]))
    except Exception:
        return None


# ---- json cache / fetch ----
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
def http_json(url, cache_path, max_age=540, headers=None):
    """Return (data, status). status: 'ok' fresh|cached-fresh, 'stale' cache after a failed fetch, 'down' no data.
    Serves cache younger than max_age WITHOUT a network hit -> cheap frequent re-renders (live countdowns)."""
    c = _load(cache_path)
    if c and (time.time()-c.get("ts", 0)) < max_age:
        return c["data"], "ok"
    try:
        import urllib.request
        req = urllib.request.Request(url, headers=headers or {"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r: d = json.load(r)
        _save(cache_path, {"ts": time.time(), "data": d})
        return d, "ok"
    except Exception:
        if c: return c["data"], "stale"
        return None, "down"

# ---- icons ----
def icon(url):
    """Download+cache an image URL once; return a PIL RGBA image (or None)."""
    if not url: return None
    os.makedirs(ICON_CACHE, exist_ok=True)
    p = os.path.join(ICON_CACHE, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
    if not os.path.exists(p):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r: open(p, "wb").write(r.read())
        except Exception:
            return None
    try:
        return Image.open(p).convert("RGBA")
    except Exception:
        return None
def paste_center(img, pil, cx, cy, box):
    if not pil: return
    pil = pil.copy(); pil.thumbnail((box, box))
    img.paste(pil, (int(cx-pil.width/2), int(cy-pil.height/2)), pil)

# ---- text ----
def fit_font(d, text, path, size, minsz, maxw):
    while size > minsz and d.textlength(text, font=F(path, size)) > maxw: size -= 2
    return F(path, size)
def wrap(d, text, font, maxw):
    lines, cur = [], ""
    for w in text.split():
        t = (cur+" "+w).strip()
        if d.textlength(t, font=font) <= maxw: cur = t
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines
def ellip(d, text, font, maxw):
    if d.textlength(text, font=font) <= maxw: return text
    while text and d.textlength(text+"…", font=font) > maxw: text = text[:-1]
    return text+"…"

# ---- canvas ----
def new_canvas(top=(15,20,32), bot=(9,11,18)):
    """Return (img, draw) with the standard vertical-gradient left panel already painted.
    Defaults to the cool weather tint; pass warm (20,16,12) for salmon-style screens."""
    img = Image.new("RGB", (W, H), bot)
    grad = bytearray()
    for y in range(H):
        t = y/H
        grad += bytes(int(top[i]+(bot[i]-top[i])*t) for i in range(3))
    img.paste(Image.frombytes("RGB", (1, H), bytes(grad)).resize((PANEL_W, H)), (0, 0))
    return img, ImageDraw.Draw(img)
def header(d, title, title_font=70):
    """Standard screen header: big title top-left + CPU temp top-right."""
    d.text((PAD, 66), title, font=F(FB, title_font), fill=FG)
    # CPU temp is a live OSD overlay drawn by the lua (consistent across all screens), not baked here.

# ---- internet status (shared: drawn by finish() so EVERY screen shows it) ----
NET_STATE = DIR + "/net_state.json"
NETMON_LIVE = DIR + "/screens/net/live.json"   # written every 30s by netmon.service
NETMON_MAX_AGE = 150            # 5 probe cycles; beyond this netmon is not reporting
NETMON_OK = ("up", "icmp_blocked")             # ICMP filtered but DNS working = still online

def net_online(ttl=180):
    """Return (online, last_ok), preferring netmon.service's result over pinging again.

    netmon (screens/net/netmon.py) already probes the gateway, 1.1.1.1, 9.9.9.9 and DNS
    every 30 s for the Network screen, so every screen now reads its live.json instead of
    firing a second, slower ping of its own: no duplicate traffic, and the banner reacts
    within 30 s instead of up to `ttl`=180 s. `last_ok` also gets sharper — netmon's
    `since` is the moment the outage began, where the old code could only report the last
    time a render happened to ping successfully.

    Uses netmon's DEBOUNCED state (two consecutive bad cycles), not its per-cycle verdict:
    a single dropped ICMP packet is normal on this uplink and should not flash
    "No internet connection" across every screen. Change NETMON_OK/the key read below to
    d["raw"] if you want the twitchier per-packet behaviour.

    Falls back to the old cached self-ping when netmon is stopped or its file is stale --
    otherwise a dead monitor would silently disable the banner everywhere.
    """
    st = _load(NET_STATE) or {}
    now = time.time()
    d = _load(NETMON_LIVE)
    if d and (now - d.get("t", 0)) < NETMON_MAX_AGE:
        online = d.get("state") in NETMON_OK
        if online:
            st["last_ok"] = now
        else:
            since = d.get("since")                      # when the outage started
            if since and since > st.get("last_ok", 0):
                st["last_ok"] = since
        st["checked"] = now
        st["online"] = online
        st["src"] = "netmon"
        _save(NET_STATE, st)
        return online, st.get("last_ok")
    # --- netmon not reporting: fall back to the original self-ping ---
    if "online" in st and (now - st.get("checked", 0)) < ttl:
        return st["online"], st.get("last_ok")          # reuse recent result, no ping
    try:
        online = subprocess.call(["ping", "-c", "1", "-W", "2", "1.1.1.1"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5) == 0
    except Exception:
        online = False
    st["checked"] = now
    st["online"] = online
    st["src"] = "ping"
    if online:
        st["last_ok"] = now
    _save(NET_STATE, st)
    return online, st.get("last_ok")

def _fmt_last_online(ts):
    if not ts:
        return "unknown"
    dt = datetime.datetime.fromtimestamp(ts, ZoneInfo(TZ))
    now = datetime.datetime.now(ZoneInfo(TZ))
    return dt.strftime("%H:%M" if dt.date() == now.date() else "%a %d %b %H:%M")

def offline_text(last_ok):
    """The banner's one line of text. Drawn by banner.py, not baked into panels."""
    return "No internet connection · last online " + _fmt_last_online(last_ok)

# NOTE: the offline banner used to be painted into every screen's panel.bgra here
# (draw_offline_banner(), called by finish()). That was wrong for the same reason the
# baked-per-screen CPU temp was wrong: only the screen that happens to re-render gets
# the current state, so during a short outage the banner showed on the active screen
# and disappeared the moment you switched screens (non-active panels only re-render on
# the 600 s sweep). It is now ONE bitmap -- banner.py writes banner.bgra and kiosk.lua
# overlays it as id 2 (under the dim, above the temp badge), so it is instantly correct
# on all 8 screens and needs no panel re-render at all.
# Screens must still keep bottom content above y=1022, since the overlay covers y1022-1080.

# ---- output ----
def _atomic(path, data):
    """Write to a PID-UNIQUE temp file, then rename it into place.

    mpv's overlay-add mmaps exactly w*h*stride bytes of the file; if the file is shorter at
    that instant, mpv dies with SIGBUS ("Bus error"). The temp name MUST be unique per writer:
    with a shared "<path>.tmp" two concurrent renders end up on ONE temp inode, so writer B's
    open(...,"wb") (O_TRUNC) can zero the inode writer A has already renamed onto the live
    path -- measured on the Pi: the visible dim.bgra hit 0 bytes, then refilled in ~1 MB steps
    over 30-50 ms. That is why the 2026-07-17 "atomic" fix still crashed on fast taps: it was
    atomic per writer, not across writers."""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)          # never leave a partial temp behind
        except OSError:
            pass
        raise


def write_dim():
    """Shared root dim.bgra. The maths AND the write gate live in sundim.py, because
    render_weather.py writes the same file and gates on the same dim_alpha.txt: the two
    have to agree on the alpha byte, so there is exactly one copy of the logic."""
    return sundim.write_dim(DIR, _atomic)


def finish(key, img, outdir):
    """Write <outdir>/panel.bgra (the overlay mpv shows) + the SHARED root dim.bgra.
    Set env WK_PNG=1 to also dump <outdir>/panel.png. `key` kept for call-site clarity.
    The offline banner is NOT drawn here any more -- it is a lua overlay (see above)."""
    left = img.crop((0, 0, CROP_W, H)).convert("RGBA")
    _atomic(f"{outdir}/panel.bgra", left.tobytes("raw", "BGRA"))
    write_dim()
    if os.environ.get("WK_PNG"):
        img.save(f"{outdir}/panel.png")
