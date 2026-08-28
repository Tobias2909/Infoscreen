#!/usr/bin/env python3
"""Special-event parsing tests for the Salmon Run screen. No network, no rendering.

Worth having because two of the three event types could not be observed when the feature was
written: bigRunSchedules was an empty list and currentFest was null, and both stay that way for
most of the year. Everything here is therefore a shape that might be wrong, missing or renamed.
The screen must ignore what it does not understand, because an exception in the parser blanks the
panel instead of degrading it.

Run: python3 test_events.py
"""
import datetime, os, sys

os.environ.setdefault("INFOSCREEN_NOLOCK", "1")     # do not fight the live renderer for the lock
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import salmon_panel as sp

now = datetime.datetime.now(datetime.timezone.utc)
D = datetime.timedelta
def iso(delta): return (now + delta).strftime("%Y-%m-%dT%H:%M:%SZ")

def doc(coop=None, fest="omit"):
    d = {"data": {"coopGroupingSchedule": {} if coop is None else coop}}
    if fest != "omit":
        d["data"]["currentFest"] = fest
    return d

FEST_OK = {"title": "T", "startTime": iso(D(days=1)), "endTime": iso(D(days=3))}

CASES = [
    ("empty document",              doc(), None),
    ("coop key missing",            {"data": {}}, None),
    ("data key missing",            {}, None),
    ("document is None",            None, None),
    ("bigrun without a setting",    doc({"bigRunSchedules": {"nodes": [
        {"startTime": iso(D(days=2)), "endTime": iso(D(days=3))}]}}), "bigrun"),
    ("bigrun with garbage times",   doc({"bigRunSchedules": {"nodes": [
        {"startTime": "not a date", "endTime": "also not"}]}}), None),
    ("bigrun with no times",        doc({"bigRunSchedules": {"nodes": [{"setting": {}}]}}), None),
    ("nodes is None",               doc({"bigRunSchedules": {"nodes": None}}), None),
    ("schedule is None",            doc({"bigRunSchedules": None}), None),
    ("event already over",          doc({"teamContestSchedules": {"nodes": [
        {"startTime": iso(D(days=-4)), "endTime": iso(D(days=-2))}]}}), None),
    ("fest without teams",          doc(fest=FEST_OK), "splatfest"),
    ("fest team without colour",    doc(fest=dict(FEST_OK, teams=[{"id": "a"}, {"id": "b"}])), "splatfest"),
    ("fest colour out of range",    doc(fest=dict(FEST_OK, teams=[{"color": {"r": 9, "g": -3, "b": .5}}])), "splatfest"),
    ("fest is a string",            doc(fest="nonsense"), None),
    ("fest without times",          doc(fest={"title": "T"}), None),
    # a running event outranks one that merely starts sooner than it ends
    ("running beats upcoming",      doc({"bigRunSchedules": {"nodes": [
        {"startTime": iso(D(hours=-2)), "endTime": iso(D(hours=5)), "setting": {}}]},
        "teamContestSchedules": {"nodes": [
        {"startTime": iso(D(hours=1)), "endTime": iso(D(hours=9)), "setting": {}}]}}), "bigrun"),
    # of two upcoming events the nearer one wins
    ("nearer upcoming wins",        doc({"bigRunSchedules": {"nodes": [
        {"startTime": iso(D(days=5)), "endTime": iso(D(days=6)), "setting": {}}]},
        "teamContestSchedules": {"nodes": [
        {"startTime": iso(D(days=2)), "endTime": iso(D(days=3)), "setting": {}}]}}), "eggstra"),
]

EXTRA = []   # checks that go beyond "which event was picked"

def check(name, cond):
    EXTRA.append((name, bool(cond)))

# team names are read when the feed has them, and dropped cleanly when it does not
named = sp.pick_event(doc(fest=dict(FEST_OK, teams=[
    {"teamName": "Palace",     "color": {"r": .914, "g": .934, "b": .224}},
    {"teamName": "Theme Park", "color": {"r": .192, "g": .698, "b": .914}},
    {"teamName": "Beach",      "color": {"r": .859, "g": .196, "b": .906}}])), now)
check("three team names parsed", [t[0] for t in named["teams"]] == ["Palace", "Theme Park", "Beach"])
check("their colours parsed",    named["teams"][1][1] == (49, 178, 233))

unnamed = sp.pick_event(doc(fest=dict(FEST_OK, teams=[
    {"color": {"r": .9, "g": .2, "b": .2}}, {"color": {"r": .2, "g": .9, "b": .2}}])), now)
check("missing names give None", all(t[0] is None for t in unnamed["teams"]))
check("colours still parsed",    len(unnamed["colors"]) == 2)

blank = sp.pick_event(doc(fest=dict(FEST_OK, teams=[{"teamName": "   ", "color": {"r": .5, "g": .5, "b": .5}}])), now)
check("whitespace name is not a name", blank["teams"][0][0] is None)

# a dark team colour has to be lifted or the name is unreadable on the card
check("dark colour lifted",   sum(sp.readable((20, 10, 60))) > sum((20, 10, 60)))
check("bright colour intact", sp.readable((240, 230, 60)) == (240, 230, 60))
check("black does not divide by zero", sp.readable((0, 0, 0)) == (0, 0, 0))
check("lift keeps hue order", (lambda c: c[2] > c[0] > c[1])(sp.readable((40, 20, 90))))

fails = 0
for name, document, want in CASES:
    try:
        ev = sp.pick_event(document, now)
        got = ev["kind"] if ev else None
        ok = got == want
        if ev:
            sp.tinted_canvas(ev)                    # a parsed event must also survive being lit
            for c in (ev.get("colors") or []):
                assert all(0 <= v <= 255 for v in c), "colour channel out of range: %r" % (c,)
    except Exception as exc:
        ok, got = False, "%s: %s" % (type(exc).__name__, exc)
    fails += not ok
    print("%-4s %-28s want=%-10s got=%s" % ("OK" if ok else "FAIL", name, want, got))

for name, ok in EXTRA:
    fails += not ok
    print("%-4s %s" % ("OK" if ok else "FAIL", name))

print("\nevent parsing:", "PASS" if not fails else "FAIL (%d)" % fails)
sys.exit(1 if fails else 0)
