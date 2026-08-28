#!/usr/bin/env python3
"""Network uptime / latency monitor for the infoscreen Network screen.

Runs as a systemd service (netmon.service), one probe cycle every 30 s:

  * ICMP to the upstream gateway (re-read from /proc/net/route each cycle -- it is a
    DHCP lease, not a constant)
  * ICMP to 1.1.1.1 and 9.9.9.9
  * a raw UDP DNS query to 1.1.1.1:53 (then 9.9.9.9:53)

All probes are bound to eth2. There is a second default route via eth1
(metric 500), so an unbound probe could leave through the wrong interface once
the upstream uplink dies and report nonsense.

Being the router itself, this box can tell apart failure modes a plain client
cannot -- that classification is the whole point of the screen:

  link_down    eth2 lost carrier
  no_lease     carrier, but no IPv4 address (DHCP gone)
  gw_down      gateway unreachable -> upstream infrastructure
  wan_down     gateway fine, nothing beyond it -> the upstream's uplink
  dns_down     internet reachable but resolvers are not
  icmp_blocked ICMP filtered while DNS still works -- NOT an outage, just noted
  up

Outputs (all in this directory):
  netlog.jsonl   append-only events: state transitions, hourly rollups,
                 service start/stop, WAN IP changes. Kept forever (tiny).
  samples.jsonl  one line per cycle for the 24 h sparkline, pruned to 3 days
                 once a day.
  live.json      current state, for the panel to read cheaply.

SD-card wear is a real constraint here, so per-cycle samples are buffered in RAM
and flushed every 10 cycles (5 min) -- 288 writes/day instead of 2880. State
transitions bypass the buffer and are written the moment they are confirmed.

On top of reporting, a watchdog reacts to one specific failure the upstream network produces
(see the WD_* block below): a confirmed outage that the link layer hides, which
only a fresh DHCP lease clears.
"""
import json, os, re, socket, struct, subprocess, sys, time, signal, random, fcntl

DIR = os.path.dirname(os.path.abspath(__file__))
NETLOG = os.path.join(DIR, "netlog.jsonl")
SAMPLES = os.path.join(DIR, "samples.jsonl")
LIVE = os.path.join(DIR, "live.json")

IFACE = "eth2"
PUB = ["1.1.1.1", "9.9.9.9"]
DNS_TARGETS = ["1.1.1.1", "9.9.9.9"]
DNS_NAME = "cloudflare.com"

CYCLE = 30          # seconds between probe cycles
PING_W = 2          # ping timeout
DNS_W = 2.0         # dns probe timeout
CONFIRM = 2         # consecutive identical raw verdicts before a state is real
FLUSH_EVERY = 10    # cycles buffered before touching the SD card
SAMPLE_KEEP = 3 * 24 * 3600
DOWN_STATES = {"link_down", "no_lease", "gw_down", "wan_down", "dns_down"}

# --- uplink watchdog ---------------------------------------------------------
# The upstream stops forwarding this box's traffic when its session/DHCP binding goes
# stale, while eth2 keeps carrier and the gateway keeps answering ARP (2026-07-26,
# 17 min). NM sees a healthy link, never re-DHCPs, and the uplink stays dead until
# the connection is bounced. The probes above already know this happened, so the
# watchdog reads that verdict instead of measuring anything itself, and re-DHCPs.
WD_ENABLE = True
# link_down is excluded on purpose: no carrier means cable/PHY/switch, and a
# bounce cannot fix that -- it would only add pointless AP downtime. dns_down is
# excluded too: resolvers are not a lease problem.
WD_STATES = {"gw_down", "wan_down", "no_lease"}
WD_AFTER = 180        # a confirmed outage must persist this long before acting
WD_GAP = 900          # min seconds between bounces...
WD_GAP_MAX = 3600     # ...doubling per consecutive attempt, capped here, so a
                      # real multi-hour upstream outage does not bounce every 15 min
WD_CMD = ["sudo", "-n", "/usr/local/sbin/wan-bounce"]

running = True


def _stop(*a):
    global running
    running = False


signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)


# --- log helpers -------------------------------------------------------------
def emit(ev, **kw):
    kw["ev"] = ev
    kw["t"] = int(time.time())
    with open(NETLOG, "a") as f:
        f.write(json.dumps(kw, separators=(",", ":")) + "\n")


def write_live(d):
    tmp = LIVE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, separators=(",", ":"))
    os.replace(tmp, LIVE)


# --- link state (no forks: ioctl + procfs) -----------------------------------
def carrier(iface=IFACE):
    try:
        with open("/sys/class/net/%s/carrier" % iface) as f:
            return f.read().strip() == "1"
    except OSError:
        return False


def ipv4(iface=IFACE):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        r = fcntl.ioctl(s.fileno(), 0x8915,  # SIOCGIFADDR
                        struct.pack("256s", iface.encode()[:15]))
        return socket.inet_ntoa(r[20:24])
    except OSError:
        return None
    finally:
        s.close()


def gateway(iface=IFACE):
    """Default gateway on iface, from /proc/net/route (little-endian hex)."""
    try:
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                p = line.split()
                if p[0] == iface and p[1] == "00000000" and int(p[3], 16) & 2:
                    return socket.inet_ntoa(struct.pack("<L", int(p[2], 16)))
    except OSError:
        pass
    return None


# --- probes ------------------------------------------------------------------
_RTT = re.compile(r"time=([\d.]+)\s*ms")


def ping(host):
    """Return RTT in ms, or None. Bound to IFACE so routing can't lie to us."""
    if not host:
        return None
    try:
        p = subprocess.run(["ping", "-n", "-q", "-c", "1", "-W", str(PING_W),
                            "-I", IFACE, host],
                           capture_output=True, text=True, timeout=PING_W + 3)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if p.returncode != 0:
        return None
    m = re.search(r"=\s*([\d.]+)/([\d.]+)/", p.stdout) or _RTT.search(p.stdout)
    return round(float(m.group(1)), 2) if m else None


def dns_probe(host, src=None):
    """Raw UDP DNS A-query. Returns RTT in ms, or None.

    Asks the upstream resolver directly rather than going through the local
    Pi-hole: this tests reachability of port 53 on the internet without being
    answered out of Pi-hole's cache, and without polluting its statistics.

    SO_BINDTODEVICE needs CAP_NET_RAW and we run unprivileged, so pinning to
    eth2 falls back to binding eth2's source address. That is enough here: the
    eth2 default route has the lower metric, and once eth2 is gone the bind
    itself fails -- which is the answer we wanted anyway.
    """
    qid = random.randint(0, 0xFFFF)
    q = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    for label in DNS_NAME.split("."):
        q += bytes([len(label)]) + label.encode()
    q += b"\x00" + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(DNS_W)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
        except OSError:
            if src:
                s.bind((src, 0))
        t0 = time.time()
        s.sendto(q, (host, 53))
        while True:
            d = s.recv(1024)
            if len(d) >= 2 and struct.unpack(">H", d[:2])[0] == qid:
                return round((time.time() - t0) * 1000, 2)
    except OSError:
        return None
    finally:
        s.close()


def classify():
    """One probe cycle -> (raw_state, measurements dict)."""
    m = {"gw": None, "pub": {}, "dns": {}, "ip": None, "gwip": None}
    if not carrier():
        return "link_down", m
    m["ip"] = ipv4()
    if not m["ip"]:
        return "no_lease", m
    m["gwip"] = gateway()
    m["gw"] = ping(m["gwip"])
    for h in PUB:
        m["pub"][h] = ping(h)
    pub_ok = any(v is not None for v in m["pub"].values())
    # DNS is only probed when it can add information: if ICMP already proves the
    # path works, one resolver check is enough to catch resolver-only failure;
    # if ICMP fails, DNS decides outage vs. ICMP-filtering.
    for h in DNS_TARGETS:
        m["dns"][h] = dns_probe(h, m["ip"])
        if m["dns"][h] is not None and pub_ok:
            break
    dns_ok = any(v is not None for v in m["dns"].values())

    if not pub_ok and not dns_ok:
        # gateway silent too -> blame the upstream infrastructure, not the uplink
        return ("gw_down" if m["gw"] is None else "wan_down"), m
    if not pub_ok and dns_ok:
        return "icmp_blocked", m
    if pub_ok and not dns_ok:
        return "dns_down", m
    return "up", m


def best_rtt(m):
    v = [x for x in m["pub"].values() if x is not None]
    if not v:
        v = [x for x in m["dns"].values() if x is not None]
    return min(v) if v else None


def wd_due(state, since, now, last_bounce, tries):
    """Is a uplink bounce due? Pure decision -- unit-tested in test_netmon.py."""
    if not WD_ENABLE or state not in WD_STATES:
        return False
    if now - since < WD_AFTER:
        return False
    gap = min(WD_GAP * 2 ** max(0, tries - 1), WD_GAP_MAX) if tries else WD_GAP
    return now - last_bounce >= gap


def wd_bounce(state, since, now, attempt):
    """Run the bounce helper (root, via sudo) and log the outcome to netlog."""
    emit("bounce", state=state, down_s=now - since, attempt=attempt)
    try:
        p = subprocess.run(WD_CMD, capture_output=True, text=True, timeout=90)
        out, rc = (p.stdout or p.stderr), p.returncode
    except (subprocess.TimeoutExpired, OSError) as e:
        out, rc = str(e), None
    emit("bounced", rc=rc, out=out.strip()[:200], attempt=attempt)


def prune_samples():
    """Keep 3 days. Runs at most once a day -- rewriting is an SD-card write."""
    try:
        cut = time.time() - SAMPLE_KEEP
        keep = []
        with open(SAMPLES) as f:
            for line in f:
                try:
                    if json.loads(line).get("t", 0) >= cut:
                        keep.append(line)
                except ValueError:
                    pass
        tmp = SAMPLES + ".tmp"
        with open(tmp, "w") as f:
            f.writelines(keep)
        os.replace(tmp, SAMPLES)
    except OSError:
        pass


def main():
    boot_id = ""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            boot_id = f.read().strip()
    except OSError:
        pass
    with open("/proc/uptime") as f:
        boot_t = int(time.time() - float(f.read().split()[0]))

    # A gap in the log is ambiguous on its own -- the box reboots nightly at
    # 05:00. Recording boot_id + boot time lets the panel draw reboot gaps as
    # "no data" grey instead of inventing a red outage every night.
    emit("start", boot_id=boot_id, boot_t=boot_t, cycle=CYCLE, iface=IFACE)

    state, since = None, int(time.time())
    raw_hist = []
    buf = []
    hour_bucket = None
    hstat = None
    last_ip, last_gw = None, None
    last_prune = 0.0
    last_bounce, wd_tries = 0, 0

    def new_hstat(h):
        return {"h": h, "probes": 0, "ok": 0, "fail": 0, "down_s": 0, "rtts": []}

    def flush_hour():
        if not hstat or not hstat["probes"]:
            return
        r = sorted(hstat["rtts"])
        p = lambda q: r[min(len(r) - 1, int(len(r) * q))] if r else None
        emit("hour", h=hstat["h"], probes=hstat["probes"], ok=hstat["ok"],
             fail=hstat["fail"], down_s=hstat["down_s"],
             loss=round(hstat["fail"] / hstat["probes"], 4),
             rtt_p50=p(0.5), rtt_p95=p(0.95),
             rtt_min=(r[0] if r else None), rtt_max=(r[-1] if r else None))

    while running:
        t0 = time.time()
        raw, m = classify()
        rtt = best_rtt(m)
        now = int(time.time())

        # --- hourly rollup ---------------------------------------------------
        h = now - now % 3600
        if hour_bucket != h:
            flush_hour()
            hour_bucket, hstat = h, new_hstat(h)
        hstat["probes"] += 1
        if raw in DOWN_STATES:
            hstat["fail"] += 1
            hstat["down_s"] += CYCLE
        else:
            hstat["ok"] += 1
            if rtt is not None:
                hstat["rtts"].append(rtt)

        # --- debounced state -------------------------------------------------
        raw_hist.append(raw)
        del raw_hist[:-CONFIRM]
        if len(raw_hist) == CONFIRM and len(set(raw_hist)) == 1 and raw != state:
            prev, prev_since = state, since
            state, since = raw, now - (CONFIRM - 1) * CYCLE
            emit("state", to=state, **{"from": prev}, since=since,
                 gw=m["gw"], pub=m["pub"], dns=m["dns"], ip=m["ip"],
                 gwip=m["gwip"],
                 # how long the state we just left had lasted -- this is what the
                 # panel's outage list reads to get each outage's duration
                 dur=(since - prev_since if prev is not None else None))

        # --- uplink watchdog (acts on the debounced state, never on raw) ------
        if state in WD_STATES:
            if wd_due(state, since, now, last_bounce, wd_tries):
                wd_tries += 1
                last_bounce = now
                wd_bounce(state, since, now, wd_tries)
        elif wd_tries:
            wd_tries = 0          # recovered: next outage starts at WD_GAP again

        # --- WAN IP / gateway churn (more frequent than real outages here) ---
        if m["ip"] and m["ip"] != last_ip:
            if last_ip is not None:
                emit("wanip", ip=m["ip"], prev=last_ip)
            last_ip = m["ip"]
        if m["gwip"] and m["gwip"] != last_gw:
            if last_gw is not None:
                emit("gwip", gwip=m["gwip"], prev=last_gw)
            last_gw = m["gwip"]

        # --- buffered per-cycle sample ---------------------------------------
        buf.append({"t": now, "s": raw, "r": rtt, "g": m["gw"],
                    "d": next((v for v in m["dns"].values() if v is not None), None)})
        if len(buf) >= FLUSH_EVERY:
            try:
                with open(SAMPLES, "a") as f:
                    for b in buf:
                        f.write(json.dumps(b, separators=(",", ":")) + "\n")
                buf = []
            except OSError:
                del buf[:-FLUSH_EVERY * 4]   # never grow without bound
            if time.time() - last_prune > 86400:
                prune_samples()
                last_prune = time.time()

        write_live({"t": now, "state": state or raw, "raw": raw, "since": since,
                    "rtt": rtt, "gw": m["gw"], "ip": m["ip"], "gwip": m["gwip"],
                    "pub": m["pub"], "dns": m["dns"], "cycle": CYCLE,
                    "boot_t": boot_t, "boot_id": boot_id})

        slept = time.time() - t0
        for _ in range(int(max(0.0, CYCLE - slept))):
            if not running:
                break
            time.sleep(1)

    # graceful stop (systemd SIGTERM, incl. the nightly 05:00 reboot) so the
    # panel can render the gap as planned downtime rather than an outage
    if buf:
        try:
            with open(SAMPLES, "a") as f:
                for b in buf:
                    f.write(json.dumps(b, separators=(",", ":")) + "\n")
        except OSError:
            pass
    flush_hour()
    emit("stop", state=state)


if __name__ == "__main__":
    main()
