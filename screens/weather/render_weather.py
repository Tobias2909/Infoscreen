#!/usr/bin/env python3
import json, urllib.request, datetime, os, sys, time, fcntl, signal
from PIL import Image, ImageDraw, ImageFont
# One render at a time: a tap storm used to stack instances of this script, and two concurrent
# writers of one .bgra is what made mpv die with SIGBUS (see _atomic_w). kiosk_common does the
# same for the other screens; this one is standalone by design, so it locks inline. The alarm
# stops a wedged fetch from holding the lock (= freezing this screen) forever.
if not os.environ.get("INFOSCREEN_NOLOCK"):
    _LOCK=open("/tmp/infoscreen-%s.lock"%(os.path.basename(sys.argv[0]) or "render_weather.py"),"w")
    try: fcntl.flock(_LOCK, fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError: sys.exit(0)
    def _deadline(sig, frame): raise SystemExit("render_weather: deadline exceeded, aborting")
    signal.signal(signal.SIGALRM, _deadline)
    signal.alarm(300)   # >> the 60 s open-meteo attempt + met.no fallback
W,H=1920,1080; PANEL_W=1180
DIR=os.path.dirname(os.path.abspath(__file__))
ROOT=os.path.dirname(os.path.dirname(DIR))  # infoscreen root (shared dim.bgra)
sys.path.insert(0, ROOT)          # for sundim only. NOT kiosk_common: its import-time
import sundim                     # _single_instance() would grab this script's own lock
from sundim import LAT, LON, TZ, LABEL, UA   # file on a second fd and exit(0). See sundim.py.
OUT=DIR+"/panel.png"
CACHE=DIR+"/weather_cache.json"   # last-good API response + epoch ts
STATE=DIR+"/fetch_state.json"     # {last_attempt, fail_count} for backoff
SLOTS=DIR+"/slot_cache.json"      # rolling {iso_local_hour: [temp,code]} history to backfill slots a provider's current feed lacks (e.g. past-morning hours)
MAX_BACKOFF=3600   # cap retry spacing at 1h when API is down (avoid hammering / rate-limit ban)
FB="/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FR="/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FL="/usr/share/fonts/truetype/dejavu/DejaVuSans-ExtraLight.ttf"
def F(p,s): return ImageFont.truetype(p,s)
def net_up():
    # Single quick ping to distinguish "weather providers down but net works" from "no network at all".
    # Called ONLY when both providers already failed (the red branch), so cost is negligible.
    import subprocess
    try:
        subprocess.run(["ping","-c","1","-W","2","1.1.1.1"],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=4,check=True)
        return True
    except Exception: return False
ACC=(127,209,255); FG=(238,242,248); SUB=(150,163,180); CLOUD=(176,186,200); WARN=(255,180,70); ERR=(255,70,70)
def wmo(c):
    if c==0: return("Clear","sun")
    if c==1: return("Mainly clear","partly")
    if c==2: return("Partly cloudy","partly")
    if c==3: return("Overcast","cloud")
    if c in(45,48): return("Fog","fog")
    if c in(51,53,55,56,57): return("Drizzle","rain")
    if c in(61,63,65,66,67): return("Rain","rain")
    if c in(71,73,75,77,85,86): return("Snow","snow")
    if c in(80,81,82): return("Showers","rain")
    if c in(95,96,99): return("Thunderstorm","storm")
    return("—","cloud")
def fetch():
    url=("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
         "&current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m"
         "&hourly=temperature_2m,weather_code&timezone=%s&forecast_days=3")%(LAT,LON,TZ)
    with urllib.request.urlopen(url,timeout=60) as r: return json.load(r)
def _metno_wmo(code):
    # Map met.no symbol_code (e.g. "partlycloudy_day") to the WMO code wmo() expects.
    b=code.replace("_day","").replace("_night","").replace("_polartwilight","")
    if "thunder" in b: return 95
    if "snow" in b: return 73
    if "sleet" in b: return 66
    if "drizzle" in b: return 51
    if "rain" in b: return 80 if "showers" in b else 63
    if b=="fog": return 45
    if b=="cloudy": return 3
    if b=="partlycloudy": return 2
    if b=="fair": return 1
    if b=="clearsky": return 0
    return 3
def fetch_metno():
    # FALLBACK provider (open-meteo keeps degrading: slow-stall + intermittent 502).
    # Returns data in open-meteo SHAPE (current{} + hourly{time[],temperature_2m[],weather_code[]})
    # so every downstream consumer (wmo/hidx/slot) is unchanged. met.no requires a User-Agent;
    # its compact feed has no feels-like, so apparent_temperature falls back to air temp.
    from zoneinfo import ZoneInfo
    url=("https://api.met.no/weatherapi/locationforecast/2.0/compact?lat=%s&lon=%s")%(LAT,LON)
    req=urllib.request.Request(url,headers={"User-Agent":UA})   # met.no 403s without an identifying UA
    with urllib.request.urlopen(req,timeout=30) as r: j=json.load(r)
    ts=j["properties"]["timeseries"]
    tz=ZoneInfo(TZ)
    def loc(t): return datetime.datetime.fromisoformat(t.replace("Z","+00:00")).astimezone(tz)
    def sym(e):
        for k in("next_1_hours","next_6_hours","next_12_hours"):
            s=e["data"].get(k,{}).get("summary",{}).get("symbol_code")
            if s: return s
        return "cloudy"
    times=[]; temps=[]; codes=[]
    for e in ts:
        det=e["data"]["instant"]["details"]
        times.append(loc(e["time"]).strftime("%Y-%m-%dT%H:00"))  # met.no times are UTC -> local to match hidx
        temps.append(det.get("air_temperature"))
        codes.append(_metno_wmo(sym(e)))
    cd=ts[0]["data"]["instant"]["details"]
    current={
        "temperature_2m": cd.get("air_temperature"),
        "apparent_temperature": cd.get("air_temperature"),   # compact feed has no feels-like
        "relative_humidity_2m": round(cd.get("relative_humidity",0)),
        "weather_code": _metno_wmo(sym(ts[0])),
        "wind_speed_10m": round(cd.get("wind_speed",0)*3.6),  # m/s -> km/h (open-meteo default unit)
    }
    return {"current":current,"hourly":{"time":times,"temperature_2m":temps,"weather_code":codes}}
def _load(p):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return None
def _save(p,o):
    try:
        with open(p,"w") as f: json.dump(o,f)
    except Exception: pass
def update_slots(hourly):
    # Merge this fetch's hourly forecast into a rolling per-hour history, keyed by local ISO hour.
    # Fresher values overwrite; hours from days already over are pruned. Lets a slot first seen via
    # open-meteo (e.g. today 08:00) survive after open-meteo dies and met.no's feed no longer carries
    # that past hour. Returns the merged dict for rendering.
    sc=_load(SLOTS) or {}
    times=hourly.get("time",[]); ht=hourly.get("temperature_2m",[]); hc=hourly.get("weather_code",[])
    for i,t in enumerate(times):
        if i<len(ht) and i<len(hc) and ht[i] is not None and hc[i] is not None:
            sc[t]=[ht[i],hc[i]]
    today=datetime.date.today().isoformat()
    sc={k:v for k,v in sc.items() if k[:10]>=today}   # drop hours belonging to days already over
    _save(SLOTS,sc)
    return sc
def get_weather():
    # Returns (data, cache_ts_or_None, status, err, src, om_ok).
    # status: "ok" fresh | "down" both providers failed (serving cache) | "backoff" skipped network (serving cache)
    # src: which provider the returned data came from ("open-meteo"/"met.no"), persisted in CACHE so stale serves show it too.
    # om_ok: epoch of last SUCCESSFUL open-meteo (primary) fetch, persisted in STATE (om_last_ok). Stamps the orange
    #        "open-meteo down" line so user sees when the primary last worked. NOT the cache ts (that updates on met.no too).
    st=_load(STATE) or {"last_attempt":0,"fail_count":0}
    cache=_load(CACHE)
    now=time.time()
    fc=int(st.get("fail_count",0))
    om_ok=st.get("om_last_ok")
    backoff=min(MAX_BACKOFF, 600*(2**min(fc,3)))   # 600 -> 1200 -> 2400 -> 4800(cap 3600)
    # In a backoff window after failures: serve cache, make NO network request.
    if fc>0 and (now-float(st.get("last_attempt",0)))<backoff:
        if cache: return cache["data"], cache["ts"], "backoff", None, cache.get("src"), om_ok
        return None, None, "backoff", "no cached data yet", None, om_ok
    # Try open-meteo (primary, has feels-like) then met.no (fallback) before declaring an outage.
    data=None; last_err=None; src=None
    try:
        data=fetch(); src="open-meteo"
    except Exception as e1:
        try:
            data=fetch_metno(); src="met.no"
        except Exception as e2:
            last_err="open-meteo:%s | met.no:%s"%(e1,e2)
    if data is not None:
        if src=="open-meteo": om_ok=now   # primary succeeded -> stamp its last-good time
        _save(CACHE,{"ts":now,"data":data,"src":src})
        _save(STATE,{"last_attempt":now,"fail_count":0,"om_last_ok":om_ok})
        return data, now, "ok", None, src, om_ok
    st["last_attempt"]=now; st["fail_count"]=fc+1; st["om_last_ok"]=om_ok
    _save(STATE,st)
    if cache: return cache["data"], cache["ts"], "down", last_err, cache.get("src"), om_ok
    return None, None, "down", last_err, None, om_ok
def sun(d,cx,cy,r,col=(255,201,70)):
    import math
    for i in range(8):
        a=i*math.pi/4
        d.line([cx+math.cos(a)*r*1.35,cy+math.sin(a)*r*1.35,cx+math.cos(a)*r*1.9,cy+math.sin(a)*r*1.9],fill=col,width=int(max(3,r/8)))
    d.ellipse([cx-r,cy-r,cx+r,cy+r],fill=col)
def cloud(d,cx,cy,r,col=CLOUD):
    d.ellipse([cx-r*1.6,cy-r*0.6,cx-r*0.2,cy+r*0.8],fill=col)
    d.ellipse([cx-r*0.7,cy-r,cx+r*0.9,cy+r*0.7],fill=col)
    d.ellipse([cx+r*0.1,cy-r*0.4,cx+r*1.6,cy+r*0.8],fill=col)
    d.rectangle([cx-r*1.5,cy+r*0.1,cx+r*1.5,cy+r*0.8],fill=col)
def moon(d,cx,cy,r,col=(225,230,242),bg=(10,12,20)):
    d.ellipse([cx-r,cy-r,cx+r,cy+r],fill=col)
    d.ellipse([cx-r+r*0.55,cy-r,cx+r+r*0.55,cy+r],fill=bg)
def icon(d,cx,cy,r,kind):
    if kind=="sun": sun(d,cx,cy,r)
    elif kind=="partly": sun(d,cx-r*0.5,cy-r*0.5,r*0.6); cloud(d,cx+r*0.2,cy+r*0.2,r*0.95)
    elif kind=="cloud": cloud(d,cx,cy,r)
    elif kind=="fog":
        cloud(d,cx,cy-r*0.2,r*0.9)
        for i in range(3): d.line([cx-r*1.4,cy+r*0.9+i*r*0.4,cx+r*1.4,cy+r*0.9+i*r*0.4],fill=SUB,width=int(max(3,r/8)))
    elif kind=="rain":
        cloud(d,cx,cy-r*0.2,r)
        for i in range(3): d.line([cx-r*0.7+i*r*0.7,cy+r*0.9,cx-r*0.9+i*r*0.7,cy+r*1.5],fill=ACC,width=int(max(4,r/7)))
    elif kind=="snow":
        cloud(d,cx,cy-r*0.2,r)
        for i in range(3): d.ellipse([cx-r*0.8+i*r*0.7,cy+r*1.0,cx-r*0.55+i*r*0.7,cy+r*1.25],fill=(230,240,255))
    elif kind=="storm":
        cloud(d,cx,cy-r*0.2,r)
        d.polygon([(cx,cy+r*0.7),(cx-r*0.4,cy+r*1.5),(cx-r*0.05,cy+r*1.4),(cx-r*0.3,cy+r*2.1),(cx+r*0.4,cy+r*1.2),(cx+r*0.05,cy+r*1.3)],fill=(255,201,70))
def _atomic_w(path, data):
    """PID-UNIQUE temp file + rename. overlay-add mmaps w*h*stride bytes, so the visible file
    must never be short -- and a SHARED "<path>.tmp" is not enough: two concurrent renders land
    on one temp inode, where B's open(...,"wb") zeroes the inode A just renamed into place.
    Mirrors kiosk_common._atomic (kept separate: this script is standalone by design)."""
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise

def _write_dim_w():
    """Shared dim.bgra. Maths + write gate live in sundim.py: kiosk_common writes the same
    file and the two must agree on the alpha byte, so there is exactly one copy."""
    sundim.write_dim(ROOT, _atomic_w)


def main():
    data, cts, status, err, src, om_ok = get_weather()
    cur = data["current"] if data else None
    HRS = update_slots(data["hourly"]) if data else (_load(SLOTS) or {})  # merged per-hour history for slot backfill
    img=Image.new("RGB",(W,H),(12,16,24))
    top=(15,20,32); bot=(9,11,18)
    for y in range(H):
        t=y/H; img.paste(tuple(int(top[i]+(bot[i]-top[i])*t) for i in range(3)),(0,y,PANEL_W,y+1))
    d=ImageDraw.Draw(img); PAD=90
    d.text((PAD,66),LABEL,font=F(FB,70),fill=FG)   # place name from location.json
    now=datetime.datetime.now().strftime("%a  %d %b")
    d.text((PAD,160),now,font=F(FR,34),fill=SUB)
    # CPU temp is a live OSD overlay drawn by the lua (consistent across screens), not baked here.
    if cur:
        temp=round(cur["temperature_2m"]); cond,kind=wmo(cur["weather_code"])
        feels=round(cur["apparent_temperature"]); hum=cur["relative_humidity_2m"]; wind=round(cur["wind_speed_10m"])
        d.text((PAD,250),f"{temp}°",font=F(FB,300),fill=FG)
        icon(d,PANEL_W-260,430,120,kind)
        d.text((PAD,600),cond,font=F(FL,90),fill=ACC)
        d.text((PAD,720),f"Feels like {feels}°     Wind {wind} km/h     Humidity {hum}%",font=F(FR,40),fill=SUB)
        td=datetime.date.today(); tom=td+datetime.timedelta(days=1)
        slots=[("Morning","08:00"),("High","MAX"),("Evening","19:00"),("Night","05:00")]  # High=daily max (hottest hour), Night=pre-dawn low
        GUT=170; x0=PAD+GUT; cw=(PANEL_W-x0-PAD)//4
        hy,ty,my=812,895,975
        d.text((PAD,ty),"Today",font=F(FR,30),fill=FG,anchor="lm")
        d.text((PAD,my),"Tomorrow",font=F(FR,28),fill=SUB,anchor="lm")
        def slot(cx,y,day,hh,ir,tf,tcol,ix,tx):
            if hh=="MAX":  # hottest hour of the day (scan all merged hours for that date)
                es=[v for k,v in HRS.items() if k.startswith(day.isoformat()+"T")]
                if not es: return
                etemp,ecode=max(es,key=lambda v:v[0])  # icon = condition at the peak hour
            else:
                dd=day+datetime.timedelta(days=1) if hh=="05:00" else day  # Night = next pre-dawn (upcoming night low)
                e=HRS.get(f"{dd.isoformat()}T{hh}")  # from merged history; backfills hours the live feed dropped
                if not e: return
                etemp,ecode=e
            tt,kk=wmo(ecode)
            if hh=="05:00":  # Night: if ANY hour over the night rains, show rain (exact time doesn't matter). Temp stays the pre-dawn low.
                night=[f"{day.isoformat()}T{h:02d}:00" for h in(20,21,22,23)]+[f"{dd.isoformat()}T{h:02d}:00" for h in range(0,7)]
                if any(wmo(HRS[k][1])[1] in("rain","storm") for k in night if k in HRS): kk="rain"
            if hh=="05:00" and kk in("sun","partly"):
                moon(d,cx+ix,y,ir)
                if kk=="partly": cloud(d,cx+ix+ir*0.7,y+ir*0.4,ir*0.8)
            else: icon(d,cx+ix,y,ir,kk)
            d.text((cx+tx,y),f"{round(etemp)}°",font=tf,fill=tcol,anchor="lm")
        for i,(lab,hh) in enumerate(slots):
            cx=x0+cw*i+cw//2
            d.text((cx,hy),lab,font=F(FR,32),fill=SUB,anchor="mm")
            slot(cx,ty,td,hh,26,F(FB,46),FG,-58,10)
            slot(cx,my,tom,hh,18,F(FR,36),SUB,-48,6)
        # Source line (bottom-left): shown ONLY when the primary (open-meteo) is down — otherwise nothing.
        SRCN={"open-meteo":"open-meteo","met.no":"met.no (backup)"}
        if status=="ok" and src=="met.no":   # primary down, running live on fallback
            d.ellipse([PAD,1031,PAD+18,1049],fill=WARN)
            ostamp=datetime.datetime.fromtimestamp(om_ok).strftime("%H:%M") if om_ok else "?"
            d.text((PAD+32,1040),f"Source: met.no (backup) — open-meteo down · last OK {ostamp}",font=F(FR,26),fill=WARN,anchor="lm")
        elif status!="ok" and cts:            # both providers unreachable -> serving local cache (RED). Ping to tell apart net-down vs providers-down.
            stamp=datetime.datetime.fromtimestamp(cts).strftime("%H:%M")
            d.ellipse([PAD,1031,PAD+18,1049],fill=ERR)
            if net_up():
                msg=f"No weather data · both providers unreachable · last update {stamp}"
            else:
                msg=f"No network connection · last update {stamp}"
            d.text((PAD+32,1040),msg,font=F(FB,26),fill=ERR,anchor="lm")
    else:
        d.text((PAD,420),"Weather n/a",font=F(FB,110),fill=(200,80,80))
        d.text((PAD,560),"API unreachable, retrying",font=F(FR,34),fill=WARN)
        d.text((PAD,620),(err or "")[:60],font=F(FR,26),fill=SUB)
    if os.environ.get("WK_PNG"): img.save(OUT)   # debug preview only
    left=img.crop((0,0,1190,H)).convert("RGBA")
    lb=left.tobytes("raw","BGRA")
    _atomic_w(DIR+"/panel.bgra",lb)
    _write_dim_w()
    print("OK status=%s temp=%s"%(status, cur["temperature_2m"] if cur else "-"))
main()
