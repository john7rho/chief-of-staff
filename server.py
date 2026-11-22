#!/usr/bin/env python3
"""Chief of Staff local runtime.

One long-running process (managed by launchd) that:
  * serves the dashboard at http://localhost:8787
  * fires the wake alarm written by Claude into state.json
  * sends bedtime / wind-down notifications
  * keeps the display awake (caffeinate)

Claude never needs to touch this file; it only writes state.json / todos.json.
"""
import json
import os
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / "state.json"
TODOS = ROOT / "todos.json"
CONFIG = ROOT / "config.json"
RUNTIME = ROOT / "runtime.json"  # local bookkeeping (alarm fired/snoozed etc.)
PORT = 8787

lock = threading.Lock()
alarm_proc = {"thread": None, "stop": threading.Event()}


# ---------- helpers ----------
def now():
    return datetime.now().astimezone()


def load(path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def parse(ts):
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.astimezone()
    except Exception:
        return None


def notify(title, msg, sound="Glass"):
    t = title.replace('"', "'")
    m = msg.replace('"', "'")
    subprocess.run(
        ["osascript", "-e", f'display notification "{m}" with title "{t}" sound name "{sound}"'],
        capture_output=True,
    )


def log(msg):
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------- alarm ----------
def ring(label):
    """Play the alarm until dismissed/snoozed (max N minutes), escalating volume."""
    cfg = load(CONFIG, {})
    sound = cfg.get("alarm_sound", "/System/Library/Sounds/Glass.aiff")
    max_min = cfg.get("alarm_max_minutes", 15)
    vol = cfg.get("alarm_start_volume", 40)
    stop = alarm_proc["stop"]
    notify("⏰ Wake up", label or "Time to get up", "Hero")
    subprocess.run(["osascript", "-e", "set volume without output muted"], capture_output=True)
    end = time.time() + max_min * 60
    while not stop.is_set() and time.time() < end:
        subprocess.run(["osascript", "-e", f"set volume output volume {vol}"], capture_output=True)
        subprocess.run(["afplay", sound], capture_output=True)
        vol = min(100, vol + 4)
        stop.wait(0.4)
    with lock:
        rt = load(RUNTIME, {})
        rt["ringing"] = False
        save(RUNTIME, rt)


def start_ring(label):
    if alarm_proc["thread"] and alarm_proc["thread"].is_alive():
        return
    alarm_proc["stop"] = threading.Event()
    t = threading.Thread(target=ring, args=(label,), daemon=True)
    alarm_proc["thread"] = t
    t.start()


def stop_ring():
    alarm_proc["stop"].set()


def scheduler_loop():
    while True:
        try:
            tick()
        except Exception as e:  # never die
            log(f"tick error: {e}")
        time.sleep(15)


def tick():
    state = load(STATE, {})
    cfg = load(CONFIG, {})
    with lock:
        rt = load(RUNTIME, {})
        n = now()

        # --- wake alarm ---
        alarm = state.get("alarm") or {}
        at = parse(alarm.get("time"))
        snooze_until = parse(rt.get("snooze_until"))
        key = alarm.get("time")
        if snooze_until and n >= snooze_until and not rt.get("ringing"):
            rt["snooze_until"] = None
            rt["ringing"] = True
            start_ring(alarm.get("label", "Snooze over"))
        elif (
            alarm.get("enabled", True)
            and at
            and key not in rt.get("fired", [])
            and at <= n <= at + timedelta(minutes=30)  # don't ring for stale alarms
        ):
            rt.setdefault("fired", []).append(key)
            rt["fired"] = rt["fired"][-20:]
            rt["ringing"] = True
            log(f"alarm firing for {key}")
            start_ring(alarm.get("label", "Wake up"))

        # --- bedtime nudges ---
        sleep = state.get("sleep") or {}
        bed = parse(sleep.get("bedtime"))
        if bed:
            wind = cfg.get("wind_down_minutes", 30)
            for tag, when, title, msg in [
                ("wind", bed - timedelta(minutes=wind), "🌙 Wind down",
                 f"Bedtime in {wind} min. Alarm set for {fmt(at)}." if at else f"Bedtime in {wind} min."),
                ("bed", bed, "😴 Bedtime", sleep.get("reason") or "Lights out."),
            ]:
                k = f"{tag}:{sleep.get('bedtime')}"
                if when <= n <= when + timedelta(minutes=20) and k not in rt.get("nudged", []):
                    rt.setdefault("nudged", []).append(k)
                    rt["nudged"] = rt["nudged"][-20:]
                    notify(title, msg, "Purr")
                    if tag == "bed" and (cfg.get("studio") or {}).get("sleep_display_at_bedtime"):
                        threading.Thread(target=studio, args=("sleep-display",), daemon=True).start()

        # --- BedJet schedule (follows Claude's sleep plan + home bedtime temperature) ---
        wake = parse(sleep.get("wake")) or at
        if bed and not sleep.get("away") and (cfg.get("bedjet_schedule") or {}).get("enabled"):
            chk = bed - timedelta(minutes=int((cfg.get("bedjet_schedule") or {}).get("pre_bed_minutes", 30)) + 30)
            k = f"bj-check:{chk.isoformat()}"
            if chk <= n <= chk + timedelta(minutes=15) and k not in rt.get("nudged", []):
                rt.setdefault("nudged", []).append(k)
                threading.Thread(target=bedjet_preflight, daemon=True).start()
        if bed and not sleep.get("away"):
            for tag, when, cmds, _ in bedjet_plan(bed, wake, alarm, cfg):
                k = f"{tag}:{when.isoformat()}"
                if when <= n <= when + timedelta(minutes=15) and k not in rt.get("nudged", []):
                    rt.setdefault("nudged", []).append(k)
                    rt["nudged"] = rt["nudged"][-40:]
                    threading.Thread(target=bedjet_run, args=(tag, cmds), daemon=True).start()

        # --- upcoming event heads-up ---
        lead = cfg.get("event_heads_up_minutes", 10)
        for ev in state.get("today", []):
            s = parse(ev.get("start"))
            if not s or ev.get("all_day"):
                continue
            k = f"ev:{ev.get('start')}:{ev.get('title')}"
            if s - timedelta(minutes=lead) <= n < s and k not in rt.get("nudged", []):
                rt.setdefault("nudged", []).append(k)
                rt["nudged"] = rt["nudged"][-40:]
                notify(f"In {int((s - n).total_seconds() // 60) + 1} min", ev.get("title", "Event"), "Ping")
        save(RUNTIME, rt)


DEFAULT_PROFILES = [  # chosen by outdoor temp (°F) at bedtime; first row whose max_f is above it wins
    {"max_f": 45, "name": "Cold night", "heat_f": 95, "heat_fan": 60, "cool_fan": 0, "cool_minutes": 0},
    {"max_f": 52, "name": "Chilly", "heat_f": 91, "heat_fan": 50, "cool_fan": 0, "cool_minutes": 0},
    {"max_f": 60, "name": "Cool", "heat_f": 87, "heat_fan": 40, "cool_fan": 20, "cool_minutes": 60},
    {"max_f": 68, "name": "Mild", "heat_f": 83, "heat_fan": 30, "cool_fan": 30, "cool_minutes": 120},
    {"max_f": 999, "name": "Warm night", "heat_f": None, "heat_fan": 0, "cool_fan": 45, "cool_minutes": 180},
]


def temp_at(dt):
    """Forecast outdoor temp (°F) for the hour containing dt, from the cached forecast."""
    try:
        weather()
        key = dt.strftime("%Y-%m-%dT%H:00")
        for t, v in zip(_wx.get("htimes", []), _wx.get("htemps", [])):
            if t == key:
                return v
    except Exception as e:
        log(f"temp_at error: {e}")
    return None


def bedjet_plan(bed, wake, alarm, cfg):
    """Return [(tag, when, cmds, summary)] for tonight. Pre-bed warm-up always runs unless it's a warm night."""
    bj = cfg.get("bedjet_schedule") or {}
    if not bj.get("enabled") or not bed:
        return []
    pre = int(bj.get("pre_bed_minutes", 30))
    outside = temp_at(bed) if bj.get("adapt_to_weather", True) else None
    prof = None
    if outside is not None:
        prof = next(p for p in (bj.get("weather_profiles") or DEFAULT_PROFILES) if outside < p["max_f"])
    plan = []
    if prof and prof["heat_f"] is None:  # warm night: pre-cool instead of heating
        plan.append(("bj-pre", bed - timedelta(minutes=pre), [["cool"], ["fan", str(prof["cool_fan"])], ["runtime", f"0:{pre + 5:02d}"]],
                     f"❄️ Pre-cool {prof['cool_fan']}%"))
    elif prof:
        plan.append(("bj-pre", bed - timedelta(minutes=pre),
                     [["heat"], ["temp", str(prof["heat_f"])], ["fan", str(prof["heat_fan"])], ["runtime", f"0:{pre + 5:02d}"]],
                     f"🔥 Heat {prof['heat_f']}° · fan {prof['heat_fan']}%"))
    else:  # no forecast available: fall back to fixed settings
        cmds = [[bj.get("pre_bed_mode", "heat")], ["runtime", f"0:{pre + 5:02d}"]]
        if bj.get("pre_bed_temp_f"):
            cmds.append(["temp", str(bj["pre_bed_temp_f"])])
        plan.append(("bj-pre", bed - timedelta(minutes=pre), cmds, "🔥 Heat"))
    cool_min = prof["cool_minutes"] if prof else int(bj.get("overnight_minutes", 0))
    cool_fan = prof["cool_fan"] if prof else int(bj.get("overnight_fan", 25))
    if cool_min:
        h, m = divmod(cool_min, 60)
        plan.append(("bj-bed", bed, [["cool"], ["fan", str(cool_fan)], ["runtime", f"{h}:{m:02d}"]],
                     f"❄️ Cool {cool_fan}% · {h}h{m:02d}".replace("h00", "h")))
    else:
        plan.append(("bj-bed", bed, [["off"]], "💤 Off at bedtime"))
    if bj.get("warm_before_wake") and wake and alarm.get("enabled", True):
        w = int(bj.get("warm_minutes", 10))
        plan.append(("bj-wake", wake - timedelta(minutes=w), [["heat"], ["runtime", f"0:{w + 5:02d}"]], f"☀️ Warm {w}m before alarm"))
    return plan


def bedjet_summary():
    st, cfg = load(STATE, {}), load(CONFIG, {})
    sl, al = st.get("sleep") or {}, st.get("alarm") or {}
    bed, wake = parse(sl.get("bedtime")), parse(sl.get("wake")) or parse(al.get("time"))
    if not bed:
        return {}
    if sl.get("away"):
        return {"away": True}
    outside = temp_at(bed)
    bj = cfg.get("bedjet_schedule") or {}
    prof = next((p for p in (bj.get("weather_profiles") or DEFAULT_PROFILES) if outside is not None and outside < p["max_f"]), None)
    steps = bedjet_plan(bed, wake, al, cfg)
    return {"outside_f": round(outside) if outside is not None else None, "profile": prof["name"] if prof else None,
            "steps": [{"tag": t, "at": w.isoformat(), "summary": sm} for t, w, _, sm in steps],
            "pre_minutes": int(bj.get("pre_bed_minutes", 30)),
            "cool_minutes": prof["cool_minutes"] if prof else int(bj.get("overnight_minutes", 0))}


def bedjet_preflight():
    """30 min before the warm-up: make sure the BedJet is reachable so tonight's schedule won't silently fail."""
    for attempt in (1, 2):
        r = subprocess.run([str(ROOT / "bin" / "bedjet"), "status"], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            log("bedjet preflight ok")
            return
        time.sleep(20)
    log(f"bedjet preflight failed: {r.stdout.strip()}")
    notify("BedJet unreachable", "Warm-up starts in 30 min. Check it's plugged in and the BedJet phone app is closed.", "Basso")


def bedjet_run(tag, cmds):
    """Send a sequence of BedJet commands, retrying each once (BLE can be flaky)."""
    for c in cmds:
        for attempt in (1, 2):
            r = subprocess.run([str(ROOT / "bin" / "bedjet"), *c], capture_output=True, text=True, timeout=60)
            log(f"bedjet {tag} {' '.join(c)}: {r.stdout.strip()}")
            if r.returncode == 0:
                break
            time.sleep(5)
        else:
            notify("BedJet", f"Couldn't reach the BedJet ({' '.join(c)}). Is the phone app connected?", "Basso")
            return


def fmt(dt):
    return dt.strftime("%-I:%M %p") if dt else "—"


# ---------- spotify ----------
import base64
import hashlib
import secrets
import shutil
import urllib.error
import urllib.parse
import urllib.request

SPOTIFY_TOKENS = ROOT / ".spotify_tokens.json"
SPOTIFY_REDIRECT = f"http://127.0.0.1:{PORT}/spotify/callback"
SPOTIFY_SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing "
                  "playlist-read-private user-library-read user-read-recently-played")
_pkce = {}


def sp_client_id():
    return load(CONFIG, {}).get("spotify_client_id", "")


def sp_login_url():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    st = secrets.token_urlsafe(16)
    _pkce[st] = verifier
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": sp_client_id(), "response_type": "code", "redirect_uri": SPOTIFY_REDIRECT,
        "code_challenge_method": "S256", "code_challenge": challenge, "scope": SPOTIFY_SCOPES, "state": st})


def _token_request(data):
    req = urllib.request.Request("https://accounts.spotify.com/api/token",
                                 data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=10) as r:
        tok = json.loads(r.read())
    old = load(SPOTIFY_TOKENS, {})
    tok.setdefault("refresh_token", old.get("refresh_token"))
    tok["expires_at"] = time.time() + tok.get("expires_in", 3600) - 60
    save(SPOTIFY_TOKENS, tok)
    os.chmod(SPOTIFY_TOKENS, 0o600)
    return tok


def sp_callback(code, st):
    verifier = _pkce.pop(st, None)
    if not verifier:
        raise ValueError("bad state")
    _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": SPOTIFY_REDIRECT,
                    "client_id": sp_client_id(), "code_verifier": verifier})


def sp_token():
    tok = load(SPOTIFY_TOKENS, {})
    if not tok.get("refresh_token"):
        return None
    if time.time() > tok.get("expires_at", 0):
        tok = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                              "client_id": sp_client_id()})
    return tok["access_token"]


def sp(method, path, body=None, params=None):
    t = sp_token()
    if not t:
        raise PermissionError("spotify not connected")
    url = "https://api.spotify.com/v1" + path + ("?" + urllib.parse.urlencode(params) if params else "")
    data = json.dumps(body).encode() if body is not None else (b"" if method in ("PUT", "POST") else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {t}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="ignore")[:300]
        raise RuntimeError(f"spotify {e.code}: {msg}")


def sp_devices():
    return sp("GET", "/me/player/devices").get("devices", [])


def sp_ensure_device(device_id=None):
    """Return a device id to play on; launches the Spotify Mac app if nothing is available."""
    if device_id:
        return device_id
    devs = sp_devices()
    active = [d for d in devs if d.get("is_active")]
    if active:
        return active[0]["id"]
    if not devs and Path("/Applications/Spotify.app").exists():
        subprocess.run(["open", "-g", "-a", "Spotify"])
        for _ in range(10):
            time.sleep(1.5)
            devs = sp_devices()
            if devs:
                break
    comp = [d for d in devs if d.get("type") == "Computer"]
    return (comp or devs or [{}])[0].get("id")


def sp_state():
    if not sp_client_id():
        return {"connected": False, "setup": "missing_client_id"}
    if not load(SPOTIFY_TOKENS, {}).get("refresh_token"):
        return {"connected": False, "setup": "login"}
    pb = sp("GET", "/me/player") or {}
    item = pb.get("item") or {}
    imgs = (item.get("album") or {}).get("images") or []
    return {
        "connected": True,
        "playing": pb.get("is_playing", False),
        "progress_ms": pb.get("progress_ms"),
        "item": {"name": item.get("name"), "uri": item.get("uri"), "duration_ms": item.get("duration_ms"),
                 "artists": ", ".join(a["name"] for a in item.get("artists", [])),
                 "art": imgs[-2]["url"] if len(imgs) > 1 else (imgs[0]["url"] if imgs else None)} if item else None,
        "device": pb.get("device"),
        "devices": [{"id": d["id"], "name": d["name"], "type": d["type"], "active": d["is_active"]}
                    for d in sp_devices()],
    }


def sp_search(q):
    r = sp("GET", "/search", params={"q": q, "type": "track,playlist,album,artist", "limit": 6})
    out = []
    for kind in ("tracks", "playlists", "albums", "artists"):
        for it in (r.get(kind) or {}).get("items", []) or []:
            if not it:
                continue
            imgs = it.get("images") or (it.get("album") or {}).get("images") or []
            sub = ", ".join(a["name"] for a in it.get("artists", [])) if it.get("artists") else \
                (it.get("owner") or {}).get("display_name", "")
            out.append({"kind": kind[:-1], "name": it["name"], "sub": sub, "uri": it["uri"],
                        "art": imgs[-1]["url"] if imgs else None})
    return out


def sp_cmd(b):
    a = b.get("action")
    dev = b.get("device_id")
    q = {"device_id": dev} if dev else None
    if a == "play_uri":
        dev = sp_ensure_device(dev)
        uri = b["uri"]
        body = {"uris": [uri]} if uri.startswith("spotify:track:") else {"context_uri": uri}
        sp("PUT", "/me/player/play", body, {"device_id": dev} if dev else None)
    elif a == "play":
        dev = sp_ensure_device(dev)
        sp("PUT", "/me/player/play", None, {"device_id": dev} if dev else None)
    elif a == "pause":
        sp("PUT", "/me/player/pause", None, q)
    elif a == "next":
        sp("POST", "/me/player/next", None, q)
    elif a == "prev":
        sp("POST", "/me/player/previous", None, q)
    elif a == "volume":
        sp("PUT", "/me/player/volume", None, {"volume_percent": int(b["value"]), **(q or {})})
    elif a == "transfer":
        sp("PUT", "/me/player", {"device_ids": [b["device_id"]], "play": True})
    else:
        raise ValueError("unknown action")


# ---------- weather (Open-Meteo, no key; only city-level coordinates are sent) ----------
_wx = {"at": 0, "data": None, "loc": None}
WMO = {0: ("clear", "Clear"), 1: ("clear", "Mostly clear"), 2: ("cloudy", "Partly cloudy"), 3: ("cloudy", "Overcast"),
       45: ("fog", "Fog"), 48: ("fog", "Fog"), 51: ("rain", "Drizzle"), 53: ("rain", "Drizzle"), 55: ("rain", "Drizzle"),
       56: ("rain", "Freezing drizzle"), 57: ("rain", "Freezing drizzle"), 61: ("rain", "Light rain"), 63: ("rain", "Rain"),
       65: ("rain", "Heavy rain"), 66: ("rain", "Freezing rain"), 67: ("rain", "Freezing rain"), 71: ("snow", "Light snow"),
       73: ("snow", "Snow"), 75: ("snow", "Heavy snow"), 77: ("snow", "Snow"), 80: ("rain", "Showers"), 81: ("rain", "Showers"),
       82: ("rain", "Heavy showers"), 85: ("snow", "Snow showers"), 86: ("snow", "Snow showers"), 95: ("storm", "Thunderstorm"),
       96: ("storm", "Thunderstorm"), 99: ("storm", "Thunderstorm")}


def outfit(feels, hi, lo, rain_pct, wind, kind):
    t = feels
    if t < 35: top = "Heavy coat, hat & gloves"
    elif t < 45: top = "Warm coat"
    elif t < 55: top = "Jacket or sweater"
    elif t < 65: top = "Light layer"
    elif t < 78: top = "T-shirt weather"
    else: top = "Shorts & breathable"
    extras = []
    if kind in ("rain", "storm") or rain_pct >= 50: extras.append("umbrella")
    elif rain_pct >= 30: extras.append("pack an umbrella")
    if hi - lo >= 15: extras.append("layers, big temp swing")
    if wind >= 15: extras.append("windbreaker")
    if kind == "snow": extras.append("boots")
    return top + (" · " + ", ".join(extras) if extras else "")


def nws_alerts(lat, lon):
    """Active US National Weather Service alerts for the point (empty outside the US / on error)."""
    try:
        req = urllib.request.Request(f"https://api.weather.gov/alerts/active?point={lat},{lon}",
                                     headers={"User-Agent": "ChiefOfStaff-dashboard (local)", "Accept": "application/geo+json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            feats = json.loads(r.read()).get("features", [])
    except Exception as e:
        log(f"nws alerts error: {e}")
        return []
    sev = {"Extreme": 0, "Severe": 1, "Moderate": 2, "Minor": 3}
    out = [{"event": p.get("event"), "severity": p.get("severity"), "onset": p.get("onset"),
            "ends": p.get("ends") or p.get("expires"), "headline": p.get("headline"),
            "summary": (p.get("description") or "").replace("\n", " ")[:400]}
           for p in (f["properties"] for f in feats)]
    return sorted(out, key=lambda a: sev.get(a["severity"], 9))


def weather():
    st = load(STATE, {})
    loc = st.get("location") or load(CONFIG, {}).get("location")
    if not loc:
        return {"error": "set config.json → location {name, lat, lon}"}
    key = (round(float(loc["lat"]), 2), round(float(loc["lon"]), 2))
    if _wx["data"] and _wx["loc"] == key and time.time() - _wx["at"] < 900:
        return _wx["data"]
    q = urllib.parse.urlencode({
        "latitude": key[0], "longitude": key[1], "timezone": "auto", "forecast_days": 2,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "current": "temperature_2m,apparent_temperature,weather_code,is_day,wind_speed_10m,precipitation",
        "hourly": "precipitation_probability,weather_code,temperature_2m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset"})
    with urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + q, timeout=10) as r:
        w = json.loads(r.read())
    c, dly, hr = w["current"], w["daily"], w["hourly"]
    kind, label = WMO.get(c["weather_code"], ("cloudy", "Cloudy"))
    idx = next((i for i, t in enumerate(hr["time"]) if t >= c["time"][:13]), 0)
    nxt = [{"t": hr["time"][i], "p": hr["precipitation_probability"][i] or 0, "temp": round(hr["temperature_2m"][i])}
           for i in range(idx, min(idx + 12, len(hr["time"])))]
    rain_at = next((h["t"] for h in nxt if h["p"] >= 50), None)
    hi, lo = round(dly["temperature_2m_max"][0]), round(dly["temperature_2m_min"][0])
    pmax = dly["precipitation_probability_max"][0] or 0
    data = {"place": loc.get("name", ""), "temp": round(c["temperature_2m"]), "feels": round(c["apparent_temperature"]),
            "kind": kind, "label": label, "is_day": bool(c["is_day"]), "wind": round(c["wind_speed_10m"]),
            "hi": hi, "lo": lo, "rain_pct": pmax, "rain_at": rain_at, "hours": nxt,
            "wear": outfit(c["apparent_temperature"], hi, lo, pmax, c["wind_speed_10m"], kind),
            "tomorrow": {"hi": round(dly["temperature_2m_max"][1]), "lo": round(dly["temperature_2m_min"][1]),
                         "rain_pct": dly["precipitation_probability_max"][1] or 0}}
    data["alerts"] = nws_alerts(*key)
    _wx.update(at=time.time(), data=data, loc=key, htimes=hr["time"], htemps=hr["temperature_2m"])
    return data


# ---------- health: Oura + WHOOP (direct OAuth; tokens stay in .health_tokens.json) ----------
HEALTH_TOKENS = ROOT / ".health_tokens.json"
HEALTH_SECRETS = ROOT / ".health_secrets.json"   # {"oura": {"client_id","client_secret"}, "whoop": {...}}
PROVIDERS = {
    "oura": {"auth": "https://cloud.ouraring.com/oauth/authorize", "token": "https://api.ouraring.com/oauth/token",
             "scope": "personal daily heartrate", "api": "https://api.ouraring.com"},
    "whoop": {"auth": "https://api.prod.whoop.com/oauth/oauth2/auth", "token": "https://api.prod.whoop.com/oauth/oauth2/token",
              "scope": "read:recovery read:cycles read:sleep read:profile offline", "api": "https://api.prod.whoop.com/developer"},
}
_hstate, _hlock, _hcache = {}, threading.Lock(), {"at": 0, "data": None}
UA = {"User-Agent": "ChiefOfStaff-dashboard (local)"}


def h_creds(p):
    return (load(HEALTH_SECRETS, {}) or {}).get(p) or {}


def h_redirect(p):
    # Oura only allows http redirects to "localhost" (not 127.0.0.1)
    return f"http://localhost:{PORT}/{p}/callback"


def h_login_url(p):
    st = secrets.token_hex(4)  # WHOOP requires exactly 8 chars
    _hstate[st] = p
    return PROVIDERS[p]["auth"] + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": h_creds(p).get("client_id", ""), "redirect_uri": h_redirect(p),
        "scope": PROVIDERS[p]["scope"], "state": st})


def _h_token(p, data):
    c = h_creds(p)
    data = {**data, "client_id": c.get("client_id", ""), "client_secret": c.get("client_secret", "")}
    req = urllib.request.Request(PROVIDERS[p]["token"], data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", **UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read())
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 120
    allt = load(HEALTH_TOKENS, {})
    allt[p] = {**allt.get(p, {}), **tok}
    save(HEALTH_TOKENS, allt)          # atomic write; refresh tokens are single-use
    os.chmod(HEALTH_TOKENS, 0o600)
    return allt[p]


def h_callback(p, code, st):
    if _hstate.pop(st, None) != p:
        raise ValueError("bad state")
    _h_token(p, {"grant_type": "authorization_code", "code": code, "redirect_uri": h_redirect(p)})
    _hcache["at"] = 0


def h_access(p):
    with _hlock:  # serialize refreshes: both providers invalidate old tokens on refresh
        t = load(HEALTH_TOKENS, {}).get(p) or {}
        if not t.get("access_token"):
            return None
        if time.time() > t.get("expires_at", 0) and t.get("refresh_token"):
            extra = {"scope": "offline"} if p == "whoop" else {}
            t = _h_token(p, {"grant_type": "refresh_token", "refresh_token": t["refresh_token"], **extra})
        return t["access_token"]


def h_get(p, path, params=None):
    tok = h_access(p)
    if not tok:
        return None
    url = PROVIDERS[p]["api"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}", **UA})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def oura_summary():
    today = datetime.now().date()
    rng = {"start_date": str(today - timedelta(days=3)), "end_date": str(today + timedelta(days=1))}
    ds = (h_get("oura", "/v2/usercollection/daily_sleep", rng) or {}).get("data", [])
    rd = (h_get("oura", "/v2/usercollection/daily_readiness", rng) or {}).get("data", [])
    sl = [x for x in (h_get("oura", "/v2/usercollection/sleep", rng) or {}).get("data", []) if x.get("type") == "long_sleep"]
    last = sl[-1] if sl else {}
    return {"sleep_score": ds[-1]["score"] if ds else None, "readiness": rd[-1]["score"] if rd else None,
            "day": (ds[-1]["day"] if ds else None), "hrv": last.get("average_hrv"), "rhr": last.get("lowest_heart_rate"),
            "sleep_hours": round(last["total_sleep_duration"] / 3600, 1) if last.get("total_sleep_duration") else None,
            "efficiency": last.get("efficiency"), "bed": last.get("bedtime_start"), "wake": last.get("bedtime_end")}


def whoop_summary():
    rec = ((h_get("whoop", "/v2/recovery", {"limit": 1}) or {}).get("records") or [{}])[0]
    slp = ((h_get("whoop", "/v2/activity/sleep", {"limit": 1}) or {}).get("records") or [{}])[0]
    cyc = ((h_get("whoop", "/v2/cycle", {"limit": 1}) or {}).get("records") or [{}])[0]
    rs, ss, cs = rec.get("score") or {}, slp.get("score") or {}, cyc.get("score") or {}
    stg = ss.get("stage_summary") or {}
    asleep = sum(stg.get(k, 0) for k in ("total_light_sleep_time_milli", "total_slow_wave_sleep_time_milli", "total_rem_sleep_time_milli"))
    return {"recovery": rs.get("recovery_score"), "hrv": round(rs["hrv_rmssd_milli"]) if rs.get("hrv_rmssd_milli") else None,
            "rhr": rs.get("resting_heart_rate"), "sleep_performance": ss.get("sleep_performance_percentage"),
            "sleep_hours": round(asleep / 3600000, 1) if asleep else None, "strain": round(cs["strain"], 1) if cs.get("strain") else None,
            "state": rec.get("score_state")}


def health():
    if _hcache["data"] and time.time() - _hcache["at"] < 900:
        return _hcache["data"]
    out = {}
    enabled = load(CONFIG, {}).get("health_providers", ["whoop"])
    for p, fn in (("oura", oura_summary), ("whoop", whoop_summary)):
        if p not in enabled:
            continue
        if not h_creds(p).get("client_id"):
            out[p] = {"connected": False, "setup": "missing_app"}
        elif not (load(HEALTH_TOKENS, {}).get(p) or {}).get("access_token"):
            out[p] = {"connected": False, "setup": "login"}
        else:
            try:
                out[p] = {"connected": True, **fn()}
            except urllib.error.HTTPError as e:
                out[p] = {"connected": True, "error": f"{e.code}" + (" (membership?)" if p == "oura" and e.code == 403 else "")}
            except Exception as e:
                out[p] = {"connected": True, "error": str(e)[:120]}
    _hcache.update(at=time.time(), data=out)
    return out


# ---------- nutrition (logged by Claude or the dashboard; Claude fills in estimates) ----------
NUTRITION = ROOT / "nutrition.json"   # {"targets": {...}, "entries": [{id, at, text, kcal, protein_g, carbs_g, fat_g, source}]}


def nutrition_today():
    n = load(NUTRITION, {"targets": {}, "entries": []})
    today = datetime.now().date().isoformat()
    ents = [e for e in n.get("entries", []) if (e.get("at") or "")[:10] == today]
    tot = {k: round(sum((e.get(k) or 0) for e in ents)) for k in ("kcal", "protein_g", "carbs_g", "fat_g")}
    return {"targets": n.get("targets", {}), "entries": ents, "totals": tot,
            "pending": sum(1 for e in ents if e.get("kcal") is None)}


# ---------- mac audio output ----------
SAS = shutil.which("SwitchAudioSource") or "/usr/local/bin/SwitchAudioSource"


def outputs():
    if not Path(SAS).exists():
        return {"available": False, "devices": [], "current": None}
    devs = subprocess.run([SAS, "-a", "-t", "output"], capture_output=True, text=True).stdout.split("\n")
    cur = subprocess.run([SAS, "-c", "-t", "output"], capture_output=True, text=True).stdout.strip()
    return {"available": True, "devices": [d for d in devs if d.strip()], "current": cur}


def set_output(name):
    subprocess.run([SAS, "-t", "output", "-s", name], capture_output=True)


# ---------- mac studio ----------
def studio(action):
    r = subprocess.run([str(ROOT / "bin" / "studio"), action], capture_output=True, text=True, timeout=20)
    log(f"studio {action}: rc={r.returncode} {r.stderr.strip()[:200]}")
    return r.returncode == 0, (r.stderr or r.stdout).strip()[:200]


# ---------- http ----------
class Handler(SimpleHTTPRequestHandler):
    server_version = "CoS"
    sys_version = ""
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(ROOT), **kw)

    def log_message(self, *a):
        pass

    # ---- security gate ----
    ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

    def trusted(self, write=False):
        """Reject anything not coming from this Mac's own dashboard.
        - Host check defeats DNS-rebinding (evil.com resolving to 127.0.0.1).
        - Origin check + custom header on writes defeats cross-site requests from other web pages."""
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            return False
        if self.headers.get("Host", "") not in self.ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin and origin.replace("http://", "") not in self.ALLOWED_HOSTS:
            return False
        if write and self.headers.get("X-CoS") != "1":
            return False
        return True

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def do_OPTIONS(self):  # never grant CORS preflights
        self.send_error(403)

    def do_PUT(self):
        self.send_error(405)

    do_DELETE = do_PATCH = do_PUT

    def do_GET(self):
        if not self.trusted():
            return self.send_error(403)
        if self.path.startswith("/api/runtime"):
            rt = load(RUNTIME, {})
            return self.json({"ringing": bool(rt.get("ringing")), "snooze_until": rt.get("snooze_until")})
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if u.path == "/spotify/login":
                self.send_response(302)
                self.send_header("Location", sp_login_url())
                return self.end_headers()
            if u.path == "/spotify/callback":
                sp_callback(qs.get("code", [""])[0], qs.get("state", [""])[0])
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{PORT}/")
                return self.end_headers()
            if u.path == "/api/spotify/state":
                return self.json(sp_state())
            if u.path == "/api/spotify/search":
                return self.json(sp_search(qs.get("q", [""])[0]))
            if u.path == "/api/audio/outputs":
                return self.json(outputs())
            if u.path == "/api/weather":
                return self.json(weather())
            if u.path == "/api/bedjet/plan":
                return self.json(bedjet_summary())
            if u.path in ("/oura/login", "/whoop/login"):
                self.send_response(302)
                self.send_header("Location", h_login_url(u.path.split("/")[1]))
                return self.end_headers()
            if u.path in ("/oura/callback", "/whoop/callback"):
                h_callback(u.path.split("/")[1], qs.get("code", [""])[0], qs.get("state", [""])[0])
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{PORT}/")
                return self.end_headers()
            if u.path == "/api/health":
                return self.json(health())
            if u.path == "/api/nutrition":
                return self.json(nutrition_today())
        except Exception as e:
            return self.json({"error": str(e)}, 500)
        # only serve dashboard assets, never arbitrary files
        p = self.path.split("?")[0]
        if p == "/":
            self.path = "/index.html"
        elif p not in ("/index.html", "/state.json", "/todos.json"):
            self.send_error(404)
            return
        return super().do_GET()

    def do_POST(self):
        if not self.trusted(write=True):
            return self.send_error(403)
        n = int(self.headers.get("Content-Length") or 0)
        if n > 16384:
            return self.send_error(413)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send_error(400)
        if not isinstance(body, dict):
            return self.send_error(400)
        p = self.path
        if p == "/api/spotify/cmd":
            try:
                sp_cmd(body)
                return self.json({"ok": True})
            except Exception as e:
                return self.json({"error": str(e)}, 500)
        if p in ("/api/studio/sleep-display", "/api/studio/wake-display"):
            ok, msg = studio(p.rsplit("/", 1)[1])
            return self.json({"ok": ok, "msg": msg}, 200 if ok else 500)
        if p == "/api/audio/output":
            set_output(body.get("name", ""))
            return self.json({"ok": True})
        with lock:
            if p == "/api/dismiss":
                stop_ring()
                rt = load(RUNTIME, {})
                rt.update(ringing=False, snooze_until=None)
                save(RUNTIME, rt)
            elif p == "/api/snooze":
                stop_ring()
                mins = load(CONFIG, {}).get("snooze_minutes", 9)
                rt = load(RUNTIME, {})
                rt.update(ringing=False, snooze_until=(now() + timedelta(minutes=mins)).isoformat())
                save(RUNTIME, rt)
            elif p == "/api/test-alarm":
                start_ring("Test alarm")
            elif p == "/api/todo/toggle":
                todos = load(TODOS, [])
                for t in todos:
                    if t.get("id") == body.get("id"):
                        t["done"] = not t.get("done")
                        t["done_at"] = now().isoformat() if t["done"] else None
                save(TODOS, todos)
            elif p == "/api/food/add":
                text = (body.get("text") or "").strip()[:300]
                if text:
                    n = load(NUTRITION, {"targets": {}, "entries": []})
                    n.setdefault("entries", []).append({"id": uuid.uuid4().hex[:8], "at": now().isoformat(), "text": text,
                                                        "kcal": None, "protein_g": None, "carbs_g": None, "fat_g": None,
                                                        "source": "dashboard"})
                    n["entries"] = n["entries"][-500:]
                    save(NUTRITION, n)
            elif p == "/api/food/delete":
                n = load(NUTRITION, {"targets": {}, "entries": []})
                n["entries"] = [e for e in n.get("entries", []) if e.get("id") != body.get("id")]
                save(NUTRITION, n)
            elif p == "/api/todo/add":
                text = (body.get("text") or "").strip()
                if text:
                    todos = load(TODOS, [])
                    todos.append({"id": uuid.uuid4().hex[:8], "text": text, "done": False,
                                  "added": now().isoformat(), "source": "dashboard"})
                    save(TODOS, todos)
            else:
                return self.send_error(404)
        return self.json({"ok": True})

    def json(self, obj, code=200):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    os.chdir(ROOT)
    for path, default in [(TODOS, []), (RUNTIME, {})]:
        if not path.exists():
            save(path, default)
    # keep display + system awake while this runs
    caff = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(os.getpid())])
    threading.Thread(target=scheduler_loop, daemon=True).start()
    log(f"Chief of Staff running on http://localhost:{PORT} (caffeinate pid {caff.pid})")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
