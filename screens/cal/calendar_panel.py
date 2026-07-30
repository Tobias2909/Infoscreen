#!/usr/bin/env python3
"""Calendar screen — upcoming events from one or more subscribed iCal (.ics) feeds.

Two bands stacked on the left panel: "Personal" (Google secret-iCal) on top, "Uni" (a university's
timetable ICS subscription) below. Per band: events for the NEXT 3 DAYS grouped by day
(Today / Tomorrow / weekday). Title only (+ HH:MM for timed events; all-day = no time).
If a band has NO events in the next 3 days it falls back to that feed's next 3 upcoming
events (any date), each shown small WITH its date.

SUBSCRIBE (not import): every render re-reads the live feed (cached <=1x/hour), so added/
changed/removed events show up automatically — nothing is copied into a local calendar.

Feeds come from calendars.json (secret token URLs; chmod 600). Recurrence (birthdays,
weekly lectures) is expanded via recurring_ical_events. Registered in screens.conf as:
    cal:screens/cal/calendar_panel.py:0
Runs under the venv (icalendar + recurring_ical_events) via the re-exec guard below, so the
kiosk's plain `python3 calendar_panel.py` launch still gets the deps while inheriting system
Pillow (venv built with --system-site-packages).
"""
import os, sys
_VENVROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "venv")
_VENV = os.path.join(_VENVROOT, "bin", "python3")
# The --system-site-packages venv's python3 is a SYMLINK to system python, so comparing the
# binary path is useless — detect venv-ness via sys.prefix instead. Re-exec so icalendar/
# recurring_ical_events (installed only in the venv) import while inheriting system Pillow.
if os.path.realpath(sys.prefix) != os.path.realpath(_VENVROOT) and os.path.exists(_VENV):
    os.execv(_VENV, [_VENV] + sys.argv)

import json, time, datetime, urllib.request, urllib.parse
from zoneinfo import ZoneInfo
import icalendar, recurring_ical_events
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # infoscreen root -> import kiosk_common
MYDIR = os.path.dirname(os.path.abspath(__file__))  # this screen's own dir (private caches/config/output)
from kiosk_common import (PAD, PANEL_W, TZ, UA, F, FB, FR, ACC, FG, SUB, WARN, SALMON,
                          CLOUD, LINE, ellip, new_canvas, finish)

CFG = MYDIR + "/calendars.json"
TZI = ZoneInfo(TZ)
COLORS = {"acc": ACC, "salmon": SALMON, "warn": WARN, "cloud": CLOUD, "fg": FG}
DAYS = 3                 # "next N days" window (incl. today)
FALLBACK_NEAR = 35       # first fallback search window (days) — catches a birthday within a month
FALLBACK_FAR = 400       # only if still nothing in a month (guarantees next yearly recurrence)

# ---- config ----
def load_cfg():
    """calendars.json: JSON list of {label,url,color}. Falls back to bare-URL-per-line format."""
    try:
        raw = open(CFG).read()
    except Exception:
        return []
    raw = raw.strip()
    if raw.startswith("["):
        try:
            return json.loads(raw)
        except Exception:
            pass
    # legacy: one URL per line, auto-label
    out, labels = [], ["Personal", "Uni", "Cal 3", "Cal 4"]
    cols = ["acc", "salmon", "cloud", "warn"]
    for i, ln in enumerate([l.strip() for l in raw.splitlines() if l.strip() and not l.startswith("#")]):
        out.append({"label": labels[i] if i < len(labels) else f"Cal {i+1}",
                    "url": ln, "color": cols[i % len(cols)]})
    return out

# ---- fetch (subscribe: re-read live, cached <=1x/hour) ----
def fetch_ics(url, cache_path, max_age=3600):
    """Return (ical_text, status). 'ok' fresh/cached-fresh, 'stale' cache after failed fetch, 'down' nothing."""
    c = None
    try:
        c = json.load(open(cache_path))
    except Exception:
        pass
    if c and (time.time() - c.get("ts", 0)) < max_age:
        return c["text"], "ok"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=45) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            json.dump({"ts": time.time(), "text": txt}, open(cache_path, "w"))
        except Exception:
            pass
        return txt, "ok"
    except Exception:
        if c:
            return c["text"], "stale"
        return None, "down"

# ---- Google Tasks (OAuth2; optional — active only if google_tasks_oauth.json exists) ----
TASKS_OAUTH = MYDIR + "/google_tasks_oauth.json"   # {client_id, client_secret, refresh_token}
TASKS_CACHE = MYDIR + "/tasks_cache.json"          # {ts, tasks:[{title,due}]}
TOKEN_CACHE = MYDIR + "/tasks_token.json"          # short-lived access token

def _load_json(p):
    try:
        return json.load(open(p))
    except Exception:
        return None

def _access_token(o):
    """Exchange the long-lived refresh_token for an access token; cache it until it expires."""
    tc = _load_json(TOKEN_CACHE) or {}
    if tc.get("access_token") and tc.get("exp", 0) - 60 > time.time():
        return tc["access_token"]
    data = urllib.parse.urlencode({"client_id": o["client_id"], "client_secret": o["client_secret"],
                                   "refresh_token": o["refresh_token"], "grant_type": "refresh_token"}).encode()
    with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=data),
                                timeout=30) as r:
        j = json.load(r)
    tok = j["access_token"]
    try:
        json.dump({"access_token": tok, "exp": time.time() + j.get("expires_in", 3500)}, open(TOKEN_CACHE, "w"))
    except Exception:
        pass
    return tok

def fetch_tasks(max_age=3600):
    """Incomplete, DATED Google Tasks as [{title, due 'YYYY-MM-DD'}], cached <=1x/hour (subscribe-style).
    Returns [] when not configured; serves cache on any error. Undated tasks are skipped (no place in a
    date-grouped view)."""
    c = _load_json(TASKS_CACHE)
    if c and (time.time() - c.get("ts", 0)) < max_age:
        return c["tasks"]
    o = _load_json(TASKS_OAUTH)
    if not o:
        return c["tasks"] if c else []
    try:
        H = {"Authorization": "Bearer " + _access_token(o)}
        def get(url):
            with urllib.request.urlopen(urllib.request.Request(url, headers=H), timeout=30) as r:
                return json.load(r)
        out = []
        for L in get("https://tasks.googleapis.com/tasks/v1/users/@me/lists").get("items", []):
            items = get(f"https://tasks.googleapis.com/tasks/v1/lists/{L['id']}/tasks"
                        "?showCompleted=false&maxResults=100").get("items", [])
            for it in items:
                if it.get("status") == "completed" or not it.get("due"):
                    continue
                out.append({"title": (it.get("title") or "").strip() or "(no title)", "due": it["due"][:10]})
        try:
            json.dump({"ts": time.time(), "tasks": out}, open(TASKS_CACHE, "w"))
        except Exception:
            pass
        return out
    except Exception:
        return c["tasks"] if c else []

# ---- time helpers ----
def to_local(v):
    """Normalise a DTSTART value (date or datetime) to an aware local datetime for sort/group."""
    if isinstance(v, datetime.datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=TZI)
        return v.astimezone(TZI)
    return datetime.datetime.combine(v, datetime.time.min, TZI)   # all-day (date)

def is_allday(v):
    return isinstance(v, datetime.date) and not isinstance(v, datetime.datetime)

def expand(cal, start_date, days):
    """Return sorted list of dicts {start(local dt), end(local dt), allday, title} in [start, start+days)."""
    out = []
    try:
        evs = recurring_ical_events.of(cal).between(start_date, start_date + datetime.timedelta(days=days))
    except Exception:
        evs = []
    for e in evs:
        ds = e.get("DTSTART")
        if ds is None:
            continue
        v = ds.dt
        de = e.get("DTEND")
        ev = de.dt if de is not None else v
        title = str(e.get("SUMMARY", "")).strip() or "(no title)"
        out.append({"start": to_local(v), "end": to_local(ev), "allday": is_allday(v), "title": title})
    out.sort(key=lambda x: x["start"])
    return out

def expand_multi(cals, start_date, days):
    """Expand several calendars and merge into one time-sorted event list (fuse feeds in a band)."""
    out = []
    for c in cals:
        out += expand(c, start_date, days)
    out.sort(key=lambda x: x["start"])
    return out

# ---- layout: larger fonts, dynamic fill (today -> tomorrow -> day3, truncate + indicator) ----
F_LABEL, F_DAY, F_TIME, F_TITLE = 50, 36, 42, 46
F_ALLDAY, F_NOTE, F_FBDATE, F_FBTITLE, F_IND = 27, 32, 36, 40, 34
H_LABEL, H_DAY, H_EV, H_NOTE, H_FB, H_MSG, H_IND, H_EMPTY = 86, 52, 62, 50, 60, 54, 46, 104
TASK_COL = (176, 148, 252)   # violet — distinguishes a Google Task from a calendar event
_RH = {"day": H_DAY, "ev": H_EV, "note": H_NOTE, "fb": H_FB, "msg": H_MSG, "empty": H_EMPTY, "over": H_DAY}

def _task_entry(due, title, today):
    """Make a Google Task look like an all-day event dict, flagged as a task."""
    e = {"start": datetime.datetime.combine(due, datetime.time.min, TZI),
         "end": datetime.datetime.combine(due, datetime.time.min, TZI),
         "allday": True, "title": title, "task": True, "overdue": due < today}
    return e

def band_rows(cals, status, now, tasks=None):
    """Build the ordered row list for a band: overdue tasks first, then day-headers + events/tasks
    (today->tomorrow->day3), or a fallback 'upcoming' list if there's nothing to show.
    `tasks` = list of {title, due} merged in as all-day entries with a TASK badge."""
    if not cals and not tasks:
        return [("msg", "feed unreachable" if status == "down" else "no calendar")]
    today = now.date()
    near = expand_multi(cals, today, DAYS)
    near = [e for e in near if not (not e["allday"] and e["start"].date() == today and e["end"] < now)]

    # merge Google Tasks: overdue (any past date) pinned on top, dated-in-window tasks into the day groups
    overdue = []
    for t in tasks or []:
        try:
            due = datetime.date.fromisoformat(t["due"])
        except Exception:
            continue
        e = _task_entry(due, t["title"], today)
        if e["overdue"]:
            overdue.append(e)
        elif today <= due <= today + datetime.timedelta(days=DAYS - 1):
            near.append(e)
    near.sort(key=lambda x: (x["start"], not x.get("task", False)))   # same day: tasks (all-day) before timed
    overdue.sort(key=lambda x: x["start"])

    rows = []
    if overdue:                                   # overdue tasks are ADDITIONAL, pinned on top
        rows.append(("over", "Overdue"))
        rows += [("ev", e) for e in overdue]
    if near:
        cur = None
        for e in near:
            dloc = e["start"].date()
            if dloc != cur:
                cur = dloc
                delta = (dloc - today).days
                rows.append(("day", "Today" if delta == 0 else "Tomorrow" if delta == 1
                             else dloc.strftime("%a %d %b")))
            rows.append(("ev", e))
    else:                                          # no events/tasks in next 3 days -> show upcoming
        wide = expand_multi(cals, today, FALLBACK_NEAR)
        if len(wide) < 3:
            wide = expand_multi(cals, today, FALLBACK_FAR)
        wide = [e for e in wide if not (not e["allday"] and e["start"].date() == today and e["end"] < now)]
        if wide:
            rows.append(("note", "No events in next 3 days · upcoming:"))
            rows += [("fb", e) for e in wide[:3]]
        elif not overdue:
            rows.append(("empty", None))          # truly nothing (no near, no upcoming, no overdue)
    return rows

def draw_row(d, y, row, color, maxx):
    t, p = row
    if t == "day":
        d.text((PAD, y), p, font=F(FB, F_DAY), fill=SUB)
    elif t == "over":
        d.text((PAD, y), p, font=F(FB, F_DAY), fill=WARN)   # amber "Overdue" header
    elif t in ("note", "msg"):
        d.text((PAD + (8 if t == "msg" else 0), y), p, font=F(FR, F_NOTE), fill=SUB)
    elif t == "empty":
        d.text((PAD, y), "All clear — nothing scheduled", font=F(FB, 44), fill=color)
        d.text((PAD, y + 58), "No upcoming events. Enjoy the break!", font=F(FR, 32), fill=SUB)
    elif t == "ev":
        tx = PAD + 8 + 150
        if p.get("task"):
            # violet "TASK" badge in the left column instead of a time
            bx = PAD + 8
            bf = F(FB, 22)
            bw = d.textlength("TASK", font=bf)
            d.rounded_rectangle([bx, y + 7, bx + bw + 24, y + 41], radius=8, outline=TASK_COL, width=2)
            d.text((bx + 12, y + 11), "TASK", font=bf, fill=TASK_COL)
            title = p["title"]
            if p.get("overdue"):
                title += "   · due " + p["start"].strftime("%d %b")
            d.text((tx, y), ellip(d, title, F(FR, F_TITLE), maxx - tx), font=F(FR, F_TITLE), fill=FG)
        else:
            if p["allday"]:
                d.text((PAD + 8, y + 14), "all day", font=F(FR, F_ALLDAY), fill=SUB)
            else:
                d.text((PAD + 8, y), p["start"].strftime("%H:%M"), font=F(FR, F_TIME), fill=color)
            d.text((tx, y), ellip(d, p["title"], F(FR, F_TITLE), maxx - tx), font=F(FR, F_TITLE), fill=FG)
    elif t == "fb":
        # weekday in its own fixed column, then day+month at a fixed x, so "20 Jul"/"31 Jul"
        # line up across rows despite the proportional font (Mon wider than Fri).
        d.text((PAD + 8, y), p["start"].strftime("%a"), font=F(FB, F_FBDATE), fill=color)
        d.text((PAD + 8 + 92, y), p["start"].strftime("%d %b"), font=F(FB, F_FBDATE), fill=color)
        dx = PAD + 8 + 92 + 140
        tstr = "" if p["allday"] else p["start"].strftime("%H:%M")
        d.text((dx, y), ellip(d, (tstr + "  " if tstr else "") + p["title"], F(FR, F_FBTITLE), maxx - dx),
               font=F(FR, F_FBTITLE), fill=FG)

def draw_band(d, top, height, label, color, rows, status):
    """Draw label + as many rows as fit in `height`; if rows are cut, show '+N more'."""
    maxx = PANEL_W - PAD
    d.ellipse([PAD, top + 20, PAD + 24, top + 44], fill=color)
    d.text((PAD + 42, top), label, font=F(FB, F_LABEL), fill=color)
    if status == "stale":
        d.text((maxx, top + 12), "cached — offline", font=F(FR, 24), fill=WARN, anchor="ra")
    hs = [_RH[r[0]] for r in rows]
    avail = height - H_LABEL
    k, tot = 0, 0
    while k < len(rows) and tot + hs[k] <= avail:      # greedily fit as many rows as possible
        tot += hs[k]; k += 1
    if k < len(rows):                                  # truncating: make room for '+N more' and drop any
        while k > 0 and (tot + H_IND > avail or rows[k - 1][0] in ("day", "over")):   # orphan trailing header
            k -= 1; tot -= hs[k]
    y = top + H_LABEL
    for idx in range(k):
        draw_row(d, y, rows[idx], color, maxx)
        y += hs[idx]
    if k < len(rows):
        more = sum(1 for r in rows[k:] if r[0] in ("ev", "fb"))
        if more:
            d.text((PAD + 8, y + 4), f"+{more} more", font=F(FB, F_IND), fill=color)

def render():
    now = datetime.datetime.now(TZI)
    img, d = new_canvas()
    # header: title + current date (left), CPU temp (right)
    d.text((PAD, 66), "Calendar", font=F(FB, 70), fill=FG)
    d.text((PAD, 152), now.strftime("%A, %d %B %Y"), font=F(FR, 40), fill=ACC)
    # CPU temp is a live OSD overlay drawn by the lua (consistent across screens), not baked here.

    feeds = load_cfg()[:2]   # panel fits two bands
    if not feeds:
        d.text((PAD, 420), "No calendars configured", font=F(FB, 60), fill=WARN)
        d.text((PAD, 510), "add feeds to calendars.json", font=F(FR, 32), fill=SUB)
        finish("cal", img, MYDIR); return

    # fetch + parse + build rows for each feed. Google Tasks (if configured) fold into the feed
    # flagged "tasks": true (the Personal band) as overdue-on-top + dated TASK entries.
    tasks_all = fetch_tasks() if any(f.get("tasks") for f in feeds) else []
    bands = []
    for i, feed in enumerate(feeds):
        urls = feed.get("urls") or ([feed["url"]] if feed.get("url") else [])
        cals, sts = [], []
        for j, u in enumerate(urls):
            txt, st = fetch_ics(u, MYDIR + f"/cal_cache_{i}_{j}.json")
            if txt:
                try:
                    cals.append(icalendar.Calendar.from_ical(txt)); sts.append(st)
                except Exception:
                    sts.append("down")
            else:
                sts.append(st)
        status = "down" if not cals else ("stale" if "stale" in sts else "ok")
        rows = band_rows(cals, status, now, tasks_all if feed.get("tasks") else None)
        need = H_LABEL + sum(_RH[r[0]] for r in rows)
        color = COLORS.get(str(feed.get("color", "acc")).lower(), ACC)
        bands.append({"label": feed.get("label", f"Cal {i+1}"), "color": color,
                      "rows": rows, "status": status, "need": need})

    CTOP, CBOT, GAP = 222, 1052, 30
    # Band 2 (Uni) = FIXED bottom region sized to hold exactly its label + one day header + 3 events
    # + a '+N more' indicator. Band 1 (Personal) takes ALL the space above it. Uni never moves.
    UNI_H = H_LABEL + H_DAY + 3 * H_EV + H_IND
    if len(bands) == 1:
        draw_band(d, CTOP, CBOT - CTOP, bands[0]["label"], bands[0]["color"], bands[0]["rows"], bands[0]["status"])
    else:
        uni_top = CBOT - UNI_H
        draw_band(d, CTOP, uni_top - GAP - CTOP, bands[0]["label"], bands[0]["color"],
                  bands[0]["rows"], bands[0]["status"])
        d.line([PAD, uni_top - GAP // 2, PANEL_W - PAD, uni_top - GAP // 2], fill=LINE, width=2)
        draw_band(d, uni_top, UNI_H, bands[1]["label"], bands[1]["color"],
                  bands[1]["rows"], bands[1]["status"])

    finish("cal", img, MYDIR)

render()
