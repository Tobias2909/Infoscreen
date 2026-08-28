#!/usr/bin/env python3
"""Sun-driven screen dimming: the ONE implementation, shared by kiosk_common.write_dim() and
render_weather._write_dim_w().

Those two write the SAME file (the shared root dim.bgra) and gate on the SAME state file
(dim_alpha.txt), so they have to agree on the target alpha byte down to the integer. They used
to be two independent copies of this maths kept in step by a comment. Had they ever drifted,
each would have read the other's dim_alpha.txt as "changed" and rewritten 8.3 MB on every
single render -- worse than having no gate at all.

Also the home of the machine-local location/contact config (see _location below), because it
is the one module BOTH kiosk_common and render_weather may import.

Deliberately SIDE-EFFECT FREE on import beyond reading those two config files. render_weather.py takes its own flock under the tag
"render_weather.py", so it must not import kiosk_common: that module's import-time
_single_instance() would open the very same lock file on a second fd, fail LOCK_EX|LOCK_NB and
sys.exit(0) the weather screen.
"""
import datetime, json, math, os
from zoneinfo import ZoneInfo

DIR = os.path.dirname(os.path.abspath(__file__))        # = the infoscreen root

# ---- machine-local settings: WHERE this screen hangs, and the contact string the weather APIs
# are asked to identify us by. Both describe THIS installation rather than the software, so both
# live outside the repo (see .gitignore). A missing file means the documented defaults below, so a
# fresh clone renders instead of crashing -- copy location.example.json to location.json and edit.
_DEFAULTS = {"lat": 52.5200, "lon": 13.4050, "tz": "Europe/Berlin", "label": "Berlin"}
CONTACT_DEFAULT = "https://github.com/Tobias2909/Infoscreen"


def _location(path=None):
    """Read location.json, falling back per-key to _DEFAULTS.

    Reading these two files is the ONLY import-time effect this module has, and it is read-only
    -- no locks, no writes -- so the side-effect note above still holds.
    """
    cfg = dict(_DEFAULTS)
    try:
        with open(path or DIR + "/location.json") as f:
            cfg.update({k: v for k, v in json.load(f).items() if k in _DEFAULTS})
    except (OSError, ValueError):
        pass                                # no file, or unparseable -> defaults
    return cfg


def _contact(path=None):
    # met.no answers 403 to a request that does not identify its client and give a way to reach
    # whoever runs it. A project URL satisfies that, which is why the default needs no personal
    # data; drop a contact.txt next to this file to send your own address instead.
    try:
        c = open(path or DIR + "/contact.txt").read().strip()
        if c:
            return c
    except OSError:
        pass
    return CONTACT_DEFAULT


_LOC = _location()
LAT, LON, TZ, LABEL = _LOC["lat"], _LOC["lon"], _LOC["tz"], _LOC["label"]
CONTACT = _contact()
UA = "infoscreen-pi/1.0 (%s)" % CONTACT      # every outbound fetch identifies itself with this
BRIGHT_NIGHT, BRIGHT_DAY = 0.30, 0.55   # dim floor (sun at/below horizon) .. ceiling (solar noon)
DIM_STEP = 4                            # quantise the alpha: a 1/255 step is invisible, but every
                                        # distinct value costs an 8.3 MB rewrite
W, H = 1920, 1080


def solar_brightness(lat=LAT, lon=LON, tz=TZ):
    # NOAA solar position: current elevation vs today's solar-noon max -> BRIGHT_NIGHT..BRIGHT_DAY.
    # Normalised to TODAY's noon, so the peak is season-independent; what changes with the season
    # is the shape of the ramp (wide in summer, a narrow squeeze in winter).
    now = datetime.datetime.now(ZoneInfo(tz))
    off = now.utcoffset().total_seconds() / 3600.0                  # local tz offset hours (DST-aware)
    n = now.timetuple().tm_yday
    g = 2 * math.pi / 365.0 * (n - 1 + (now.hour - 12) / 24.0)
    eot = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                    - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))      # equation of time, min
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))               # declination, rad
    tst = now.hour * 60 + now.minute + now.second / 60.0 + eot + 4 * lon - 60 * off  # true solar time, min
    ha = math.radians(tst / 4.0 - 180.0)                            # hour angle, rad
    latr = math.radians(lat)
    sin_elev = math.sin(latr) * math.sin(decl) + math.cos(latr) * math.cos(decl) * math.cos(ha)
    sin_max = math.cos(latr - decl)                                 # sin(noon elevation), ha=0
    frac = max(0.0, sin_elev) / sin_max if sin_max > 0 else 0.0
    frac = min(1.0, max(0.0, frac))
    return BRIGHT_NIGHT + (BRIGHT_DAY - BRIGHT_NIGHT) * frac


def dim_alpha(brightness=None):
    """Alpha byte for dim.bgra, snapped to DIM_STEP."""
    if brightness is None:
        brightness = solar_brightness()
    return min(255, max(0, round(255 * (1 - brightness) / DIM_STEP) * DIM_STEP))


def write_dim(root, atomic):
    """Rewrite <root>/dim.bgra only when the quantised alpha byte actually changed.

    dim.bgra is one flat colour identical for every screen, so writing it on every render cost
    8.3 MB per screen per render -- ~9.6 GB/day across 8 screens under the old 600 s sweep --
    and made all 9 writers contend on one file for no benefit. `atomic` is the caller's own
    PID-unique write-then-rename helper (kiosk_common._atomic / render_weather._atomic_w), so
    this module needs to know nothing about how either of them writes. Returns the alpha in force.
    """
    from PIL import Image
    a = dim_alpha()
    path, state = f"{root}/dim.bgra", f"{root}/dim_alpha.txt"
    try:
        if int(open(state).read().strip()) == a and os.path.getsize(path) == W * H * 4:
            return a
    except (OSError, ValueError):
        pass                                    # no/garbage state file, or dim.bgra missing
    atomic(path, Image.new("RGBA", (W, H), (0, 0, 0, a)).tobytes("raw", "BGRA"))
    atomic(state, f"{a}\n".encode())
    return a
