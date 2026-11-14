#!/usr/bin/env python3
"""Sentinel-BigEye: live camera page with recording, snapshots and a motion sensor.

For each camera, ffmpeg reads the stream and turns it into JPEG frames; this
server hands the newest frame to every browser as an MJPEG stream.
Snapshots and recordings are saved on this PC (Windows folder SAVE_ROOT).
Recordings are made from the frames the page already has, so they start at
once and can include the seconds before a motion alarm.

Usage:  python3 bigeye-view.py [--port 8770] name=url[,flip] [name=url ...]
A URL is an RTSP address or an HTTP MJPEG address (http://.../cam.mjpg).
Add ",flip" after a URL to turn that picture upside down (ceiling-mounted camera).
Add-ons (motion sensor and later AI features) live in ./addons/*.py.
"""
import argparse
import collections
import datetime
import importlib.util
import io
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOI, EOI = b"\xff\xd8", b"\xff\xd9"
HERE = os.path.dirname(os.path.abspath(__file__))
import sys as _sys
_sys.path.insert(0, HERE)
from bigeye_config import CFG, camera as cfg_camera, path as cfg_path  # noqa: E402  (one TOML file, see bigeye_config.py)
PREROLL_SECONDS = 4  # pictures kept in memory so a motion recording can start before the alarm


class Camera:
    def __init__(self, name, url, fps, quality, flip=False):
        self.name, self.url, self.fps, self.quality, self.flip = name, url, fps, quality, flip
        self.frame = None
        self.stamp = 0.0
        self.recent = collections.deque()  # (stamp, jpeg) for the last PREROLL_SECONDS
        self.cond = threading.Condition()
        self.proc = None
        self.eq = None  # (brightness -100..100, contrast -100..100) done here, for cameras without their own
        threading.Thread(target=self._run, daemon=True).start()

    def restart_reader(self):
        """Drop the local reader; it reconnects by itself two seconds later."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _publish(self, frame):
        now = time.time()
        with self.cond:
            self.frame, self.stamp = frame, now
            self.recent.append((now, frame))
            while self.recent and self.recent[0][0] < now - PREROLL_SECONDS:
                self.recent.popleft()
            self.cond.notify_all()

    @staticmethod
    def _frames(buf):
        """Split complete JPEGs off the front of buf; returns (frames, rest)."""
        out = []
        while True:
            start = buf.find(SOI)
            end = buf.find(EOI, start + 2) if start >= 0 else -1
            if start < 0 or end < 0:
                return out, buf
            out.append(buf[start:end + 2])
            buf = buf[end + 2:]

    def _run_passthrough(self):
        """Source is already MJPEG over HTTP: hand the camera's own JPEGs on untouched."""
        while True:
            try:
                with urllib.request.urlopen(self.url, timeout=15) as stream:
                    buf = b""
                    while True:
                        chunk = stream.read(16384)
                        if not chunk:
                            break
                        frames, buf = self._frames(buf + chunk)
                        for f in frames:
                            self._publish(f)
            except Exception:
                pass
            time.sleep(2)

    def _run(self):
        if self.url.startswith("http") and not self.flip and not self.url.endswith(".h265"):
            return self._run_passthrough()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.url.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp"]
        if self.url.endswith(".h265"):  # raw H.265 from the 360 bridge: no container, no timestamps
            cmd += ["-use_wallclock_as_timestamps", "1", "-f", "hevc"]
        cmd += ["-fflags", "nobuffer", "-flags", "low_delay", "-i", self.url, "-an", "-q:v", str(self.quality)]
        # Every frame the camera sends, at its own rate (--fps 0), or capped at --fps.
        cmd += ["-fps_mode", "vfr"] if not self.fps else ["-r", str(self.fps)]
        while True:
            filters = ["hflip,vflip"] if self.flip else []  # camera mounted upside down (ceiling)
            # The door camera switches between full and limited colour range; the JPEG
            # encoder refuses limited range ("Non full-range YUV"), so always convert.
            filters.append("scale=out_range=pc,format=yuvj420p")
            if self.eq and any(self.eq):
                filters.append(f"eq=brightness={self.eq[0] / 200:.3f}:contrast={1 + self.eq[1] / 100:.3f}")
            full = cmd + (["-vf", ",".join(filters)] if filters else []) + ["-f", "mjpeg", "pipe:1"]
            proc = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.proc = proc
            threading.Thread(target=self._watch, args=(proc,), daemon=True).start()
            threading.Thread(target=self._errors, args=(proc,), daemon=True).start()
            buf = b""
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                frames, buf = self._frames(buf + chunk)
                for f in frames:
                    self._publish(f)
            proc.wait()
            time.sleep(2)  # stream gone: try again

    def _errors(self, proc):
        last = 0.0
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            # H.265 decoders print one line per damaged frame; one line per 10 s is enough.
            if text and time.time() - last > 10:
                last = time.time()
                log(f"{self.name}: ffmpeg: {text[:200]}")

    def _watch(self, proc):
        """Kill a reader that has stopped delivering pictures, so it is restarted."""
        started = time.time()
        while proc.poll() is None:
            time.sleep(3)
            # The first picture can take a while (the reader waits for a key frame).
            limit = 20 if self.stamp > started else 60
            if time.time() - max(self.stamp, started) > limit:
                log(f"{self.name}: reader gave no picture for {limit} s, restarting it")
                proc.kill()
                return

    def wait_frame(self, last, timeout=10):
        with self.cond:
            self.cond.wait_for(lambda: self.stamp > last, timeout)
            return self.frame, self.stamp


CAMS = {}
LOG_FILE = cfg_path("paths", "log_file")


def log(msg):
    try:
        with open(LOG_FILE, "a") as handle:
            handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def picture_monitor():
    """Logs when a camera's picture stops and when it comes back, so freezes can be explained."""
    stalled = {}
    while True:
        now = time.time()
        for name, cam in CAMS.items():
            age = now - cam.stamp if cam.stamp else None
            is_stalled = age is None or age > 15
            if is_stalled and not stalled.get(name):
                log(f"{name}: picture stopped" + (f" (last frame {age:.0f} s ago)" if age else " (no frame yet)"))
            elif not is_stalled and stalled.get(name):
                log(f"{name}: picture back")
            stalled[name] = is_stalled
        time.sleep(2)


CONTROLS = {}  # camera name -> base URL of its bridge (camera-side controls)
FEATURES = {}  # camera name -> set of extra features: "listen", "pir", "ptz"
ALLOWED_CONTROL_PATHS = ("ir", "control/picture", "control/light", "control/audio", "pir", "status", "reboot",
                         "night", "tracking", "quality", "control/time", "control/resolution", "control/keyframe",
                         "control/sleepflags")
POST_ONLY_PATHS = ("reboot", "control/time", "control/keyframe", "control/sleepflags")
MEDIA_ASKED = {}  # camera name -> last time the bridge was asked what the camera streams
FRIGATE = None  # base URL of Frigate's API (events, thumbnails, clips), e.g. http://127.0.0.1:15000
FRIGATE_ALLOWED = re.compile(r"^api/events(\?[A-Za-z0-9_=&.,-]*)?$|^api/events/[0-9.a-z-]+/(thumbnail\.jpg|snapshot\.jpg|clip\.mp4)$")
NTFY = CFG["notify"]["ntfy_url"].rstrip("/")
NTFY_TOPIC_FILE = cfg_path("notify", "topic_file")

# Everything the page saves lands here, on the Windows side of this PC.
SAVE_ROOT = cfg_path("paths", "save_root")
FOLDERS = {"snapshots": "snapshots", "recordings": "recordings", "motion": "motion"}
MANUAL_MAX_SECONDS = 30 * 60   # a manual recording stops by itself after this
MOTION_TAIL_SECONDS = 15       # keep recording this long after the last motion
MOTION_MAX_SECONDS = 10 * 60
MOTION_PREROLL = 3
NOTIFY_GAP = 120               # at most one phone message per camera per 2 minutes

# The page itself lives next to this script (bigeye-view.html): plain HTML and
# JavaScript, built from a camera list, so no markup is assembled in Python.
PAGE_FILE = os.path.join(HERE, "bigeye-view.html")


def win_path(path):
    """/mnt/c/Users/x -> C:\\Users\\x (for messages on the page and Explorer)."""
    if path.startswith("/mnt/") and len(path) > 6:
        return path[5].upper() + ":\\" + path[7:].replace("/", "\\")
    return path


def save_dir(kind):
    day = datetime.date.today().isoformat()
    path = os.path.join(SAVE_ROOT, FOLDERS[kind], day)
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- settings

SETTINGS_FILE = cfg_path("paths", "settings_file")
SETTINGS_LOCK = threading.Lock()
SETTINGS = {}
# The 360 camera's first four positions, as the app saved them (they may point
# elsewhere after the camera recalibrates; replace them with new ones).
OLD_PRESETS = [dict(p) for p in CFG["ptz"]["seed_presets"]]


def load_settings():
    global SETTINGS
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as handle:
            SETTINGS = json.load(handle)
    except (OSError, ValueError):
        SETTINGS = {}
    for name in CAMS:
        s = SETTINGS.setdefault(name, {})
        s.setdefault("motion", {"on": False, "sens": 50, "zones": [], "notify": False, "record": True})
        if "ptz" in FEATURES.get(name, ()):
            s.setdefault("ptz", {"x": 0.5, "y": 0.5, "known": False, "invert_pan": False, "invert_tilt": False,
                                 "presets": [dict(p) for p in OLD_PRESETS]})
    save_settings()


def save_settings():
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with SETTINGS_LOCK:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(SETTINGS, handle, indent=1)
        os.replace(tmp, SETTINGS_FILE)


def settings(name):
    return SETTINGS.get(name, {})


# ---------------------------------------------------------------- recording

class Recorder:
    """Writes the camera's pictures to an H.264 .mp4 at a steady rate.

    Works from the frames already in memory: it starts at once and can begin a
    few seconds in the past (pre-roll). Plays in Windows without extra codecs.
    """
    FPS = 15

    def __init__(self, cam, path, reason, preroll=0, max_seconds=MANUAL_MAX_SECONDS, stop_after=None):
        self.cam, self.path, self.reason = cam, path, reason
        self.started = time.time()
        self.hard_stop = self.started + max_seconds
        self.stop_at = self.started + stop_after if stop_after else None
        self.preroll = preroll
        self.finished = False
        threading.Thread(target=self._run, daemon=True).start()

    def extend(self, seconds):
        self.stop_at = max(self.stop_at or 0, time.time() + seconds)

    def stop(self):
        self.stop_at = time.time()

    def left(self):
        return max(0, int(min(self.hard_stop, self.stop_at or self.hard_stop) - time.time()))

    def _run(self):
        cam, tick = self.cam, 1.0 / self.FPS
        with cam.cond:
            past = [f for f in cam.recent if f[0] >= self.started - self.preroll]
            first = cam.frame
        if first is None:
            self.finished = True
            return
        try:
            from PIL import Image
            width, height = Image.open(io.BytesIO(first)).size
        except Exception:
            width, height = 1920, 1080
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "mjpeg", "-framerate", str(self.FPS),
               "-i", "pipe:0", "-vf", f"scale={width}:{height}", "-c:v", "libx264", "-preset", "veryfast",
               "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", self.path]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            # Pre-roll: the remembered pictures, spaced as they arrived.
            if past:
                t, i = past[0][0], 0
                while t < self.started:
                    while i + 1 < len(past) and past[i + 1][0] <= t:
                        i += 1
                    proc.stdin.write(past[i][1])
                    t += tick
            next_tick = time.time()
            while time.time() < min(self.hard_stop, self.stop_at or self.hard_stop):
                proc.stdin.write(cam.frame)
                next_tick += tick
                time.sleep(max(0.0, next_tick - time.time()))
        except (BrokenPipeError, OSError) as error:
            log(f"{cam.name}: recording broke: {error}")
        finally:
            try:
                proc.stdin.close()
            except OSError:
                pass
            err = proc.stderr.read().decode(errors="replace").strip()
            proc.wait()
            if err:
                log(f"{cam.name}: recording ffmpeg: {err[:300]}")
            self.finished = True
            log(f"{cam.name}: recording saved {self.path} ({int(time.time() - self.started)} s, {self.reason})")


RECORDERS = {}  # camera name -> running Recorder
REC_LOCK = threading.Lock()


def running_recorder(name):
    rec = RECORDERS.get(name)
    return rec if rec and not rec.finished and rec.left() > 0 else None


def start_recording(name, reason, preroll=0, max_seconds=MANUAL_MAX_SECONDS, stop_after=None):
    with REC_LOCK:
        rec = running_recorder(name)
        if rec:
            return rec, False
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        kind = "motion" if reason == "motion" else "recordings"
        path = os.path.join(save_dir(kind), f"{name}-{stamp}.mp4")
        rec = Recorder(CAMS[name], path, reason, preroll, max_seconds, stop_after)
        RECORDERS[name] = rec
        return rec, True


# ---------------------------------------------------------------- 360 camera (absolute positions)

PTZ_QUIET = {}  # camera name -> time until which the motion sensor ignores the picture (camera turning)
PTZ_STEP = {"pan": 0.025, "tilt": 0.04}


def ptz_goto(name, x, y, quiet=6):
    s = settings(name)["ptz"]
    x, y = min(1.0, max(0.0, x)), min(1.0, max(0.0, y))
    with urllib.request.urlopen(f"{CONTROLS[name]}/goto?x={x:.4f}&y={y:.4f}", timeout=4) as reply:
        reply.read()
    s.update(x=round(x, 4), y=round(y, 4), known=True)
    PTZ_QUIET[name] = time.time() + quiet
    use_position_zones(name)
    save_settings()
    return s


def use_position_zones(name):
    """Ignore zones belong to a favourite position: standing on one uses its zones."""
    s, m = settings(name)["ptz"], settings(name)["motion"]
    here = next((p for p in s["presets"] if abs(p["x"] - s["x"]) < 0.006 and abs(p["y"] - s["y"]) < 0.006), None)
    if m.get("zones_for") is None and m.get("zones") and "free_zones" not in m:
        m["free_zones"] = m["zones"]  # zones drawn before positions had their own
    m["zones_for"] = here["name"] if here else None
    m["zones"] = list(here.get("zones", [])) if here else list(m.get("free_zones", []))


# ---------------------------------------------------------------- reconnect (the "fix it" button)

# Each camera's bridge on the camera machine. Restarting a bridge makes it log in to the camera
# again from scratch (and resets its back-off after many failed tries).
BRIDGE_UNITS = {n: c["unit"] for n, c in CFG["cameras"].items() if c.get("unit")}
# How to reach the camera machine; an empty list = the bridges run here (plain bash).
SSH_ROUTES = [r for r in (CFG["camera_machine"]["ssh"], CFG["camera_machine"]["ssh_fallback"]) if r] or [["bash", "-c"]]
PAGE_STARTED = time.time()
AUTOFIX_AFTER = 90        # seconds without a picture before the page restarts that bridge itself
AUTOFIX_GAP = 10 * 60     # and then not again for this long (fast retries crashed the cameras before)
LAST_FIX = {}
FIX_LOCK = threading.Lock()


def reconnect(names, only_running=False, reason="button"):
    """Restart the the camera machine bridges for these cameras, then the local readers. Returns a short report."""
    units = [BRIDGE_UNITS[n] for n in names if n in BRIDGE_UNITS]
    if not units:
        return "nothing to restart"
    cond = "systemctl --user is-active -q {u}" if only_running else "systemctl --user cat {u} >/dev/null 2>&1"
    script = "; ".join(f"if {cond.format(u=u)}; then systemctl --user restart {u} && echo '{u} restarted'; "
                       f"else echo '{u} skipped (off)'; fi" for u in units)
    with FIX_LOCK:
        report = None
        for route in SSH_ROUTES:
            try:
                out = subprocess.run(route + [script], capture_output=True, text=True, timeout=40)
            except subprocess.TimeoutExpired:
                continue
            if out.returncode == 0:
                report = out.stdout.strip().replace("\n", "; ")
                break
        now = time.time()
        for n in names:
            LAST_FIX[n] = now
            if n in CAMS:
                CAMS[n].restart_reader()
    log(f"reconnect ({reason}) {', '.join(names)}: {report or 'the camera machine not reachable'}")
    return report or "the camera machine did not answer (tablet and camera Wi-Fi both down?)"


def autofix_loop():
    """A camera without a picture for a while gets its bridge restarted, gently."""
    while True:
        time.sleep(15)
        if not SETTINGS.get("_page", {}).get("autofix", True):
            continue
        now = time.time()
        for name, cam in CAMS.items():
            if name not in BRIDGE_UNITS:
                continue
            # No picture since the page started counts too (10:44 on 2026-11-14 the door bridge
            # took video in but sent none on). only_running leaves switched-off cameras alone.
            since = cam.stamp or PAGE_STARTED
            limit = AUTOFIX_AFTER if cam.stamp else 180
            if now - since > limit and now - LAST_FIX.get(name, 0) > AUTOFIX_GAP:
                reconnect([name], only_running=True, reason=f"auto, no picture for {int(now - since)} s")


# ---------------------------------------------------------------- events and add-ons

EVENTS = collections.deque(maxlen=200)
EVENTS_FILE = os.path.join(SAVE_ROOT, "events.jsonl")
LAST_EVENT = {}   # camera name -> time of the newest motion event
LAST_NOTIFY = {}
ADDON_STATUS = {}  # camera name -> live values from add-ons (motion level ...)


def load_events():
    try:
        with open(EVENTS_FILE, encoding="utf-8") as handle:
            for line in handle.readlines()[-200:]:
                EVENTS.append(json.loads(line))
    except (OSError, ValueError):
        pass


def notify_phone(name, text):
    try:
        topic = open(NTFY_TOPIC_FILE).read().strip()
        req = urllib.request.Request(f"{NTFY}/{topic}", data=text.encode(), method="POST",
                                     headers={"Title": f"Sentinel-BigEye: {name}", "Tags": "rotating_light",
                                              "Priority": "high"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as error:
        log(f"{name}: phone message failed: {error}")


def trigger(name, info):
    """Called by add-ons. Starts or extends a recording, saves a picture, logs an event."""
    now = time.time()
    if SETTINGS.get("_page", {}).get("home"):
        return  # Home mode: the owner is in; nothing is recorded or sent
    cfg = settings(name).get("motion", {})
    policy = cfg.get("record_on", "person")
    ai_down = (ADDON_STATUS.get(name, {}).get("ai") or {}).get("down") or "ai" not in ADDONS
    if policy != "motion" and info.get("source") == "motion" and not ai_down:
        return  # plain motion is only a hint; the AI decides (person / animal)
    if cfg.get("record", True):
        rec, new = start_recording(name, "motion", preroll=MOTION_PREROLL, max_seconds=MOTION_MAX_SECONDS,
                                   stop_after=MOTION_TAIL_SECONDS)
        if not new and rec.reason == "motion":
            rec.extend(MOTION_TAIL_SECONDS)
    if now - LAST_EVENT.get(name, 0) < 30:
        LAST_EVENT[name] = now
        return
    LAST_EVENT[name] = now
    stamp = datetime.datetime.now()
    pic = os.path.join(save_dir("motion"), f"{name}-{stamp.strftime('%Y%m%d-%H%M%S')}.jpg")
    frame = CAMS[name].frame
    if frame:
        with open(pic, "wb") as handle:
            handle.write(frame)
    rec = running_recorder(name)
    event = {"cam": name, "time": round(now, 1), "source": info.get("source", "?"), "box": info.get("box"),
             "label": info.get("name") or info.get("label"), "score": info.get("score"),
             "picture": os.path.relpath(pic, SAVE_ROOT),
             "clip": os.path.relpath(rec.path, SAVE_ROOT) if rec else None}
    EVENTS.append(event)
    try:
        with open(EVENTS_FILE, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event) + "\n")
    except OSError:
        pass
    log(f"{name}: motion event ({info})")
    if cfg.get("notify") and now - LAST_NOTIFY.get(name, 0) > NOTIFY_GAP:
        LAST_NOTIFY[name] = now
        threading.Thread(target=notify_phone, args=(name, f"{(info.get('name') or ('Stranger' if info.get('label') == 'person' else info.get('label')) or 'movement').capitalize()} at {stamp.strftime('%H:%M:%S')}"),
                         daemon=True).start()


ADDON_ACTIONS = {}  # (action, camera) -> function, e.g. ("follow-learn", "cam360")


class AddonContext:
    cams = CAMS
    status = ADDON_STATUS

    @staticmethod
    def has_ptz(name):
        return "ptz" in FEATURES.get(name, ())

    @staticmethod
    def register(action, name, fn):
        ADDON_ACTIONS[(action, name)] = fn

    @staticmethod
    def ptz_state(name):
        return settings(name)["ptz"]

    @staticmethod
    def ptz_goto(name, x, y, quiet=6):
        return ptz_goto(name, x, y, quiet)

    @staticmethod
    def ptz_preset(name, preset):
        p = next((p for p in settings(name)["ptz"]["presets"] if p["name"] == preset), None)
        if p:
            ptz_goto(name, p["x"], p["y"])
        return bool(p)

    @staticmethod
    def pets():
        return SETTINGS.get("_page", {}).get("pets", {})

    @staticmethod
    def save():
        save_settings()

    @staticmethod
    def settings(name):
        return settings(name)

    @staticmethod
    def quiet(name):
        return time.time() < PTZ_QUIET.get(name, 0)

    @staticmethod
    def trigger(name, info):
        trigger(name, info)

    @staticmethod
    def log(msg):
        log(msg)


def load_addons():
    folder = os.path.join(HERE, "addons")
    loaded = []
    for file in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not file.endswith(".py") or file.startswith("_"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"bigeye_addon_{file[:-3]}", os.path.join(folder, file))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.Addon(AddonContext()).start()
            loaded.append(file[:-3])
        except Exception as error:
            log(f"add-on {file} failed to start: {error}")
    log(f"add-ons: {', '.join(loaded) or 'none'}")
    return loaded


ADDONS = []


TELEMETRY = None  # live call-home monitor (telemetry.py), started in main()
TELEMETRY_PAGE = os.path.join(HERE, "telemetry.html")
CAMMEDIA_URL = CFG["camera_machine"]["stand_in_media_url"].rstrip("/")  # the 360 stand-in cloud's caught uploads

# ---------------------------------------------------------------- web server

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        self.do_GET()

    def _send(self, code, body, ctype="text/plain; charset=utf-8", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def _query(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length < 100_000:
            return None
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return None

    def _frigate(self, rest):
        if FRIGATE is None or not FRIGATE_ALLOWED.match(rest):
            self.send_error(404)
            return
        try:
            with urllib.request.urlopen(f"{FRIGATE}/{rest}", timeout=10) as reply:
                body, ctype = reply.read(), reply.headers.get("Content-Type", "application/octet-stream")
            self._send(200, body, ctype)
        except Exception as error:
            self._send(502, str(error).encode())

    def _state(self):
        now = time.time()
        out = {}
        for name, cam in CAMS.items():
            item = {"age": max(0.0, round(now - cam.stamp, 1)) if cam.stamp else -1}
            rec = running_recorder(name)
            if rec:
                item["recording"] = {"left": rec.left(), "reason": rec.reason, "for": int(now - rec.started)}
            st = ADDON_STATUS.get(name, {})
            if st.get("level") is not None:
                item["motion"] = {"level": st["level"], "active": st.get("active", False)}
            ai = st.get("ai")
            if ai:
                item["ai"] = {"down": True} if ai.get("down") else {"found": ai["found"] if now - ai["t"] < 3 else []}
            if "ptz" in FEATURES.get(name, ()):
                p = settings(name)["ptz"]
                item["ptz"] = {"x": p["x"], "y": p["y"], "known": p["known"]}
                if st.get("follow"):
                    item["follow"] = st["follow"]
            base = CONTROLS.get(name)
            if base:
                try:
                    with urllib.request.urlopen(f"{base}/status", timeout=2) as r:
                        st = json.load(r)
                    if st.get("status_seen"):
                        item["battery"] = st.get("battery_pct")
                        item["charging"] = st.get("charging")
                except Exception:
                    pass
                # What the camera itself says it streams (iWFCam bridges only).
                if "ptz" not in FEATURES.get(name, ()):
                    try:
                        with urllib.request.urlopen(f"{base}/control/state", timeout=2) as r:
                            media = json.load(r).get("media")
                        if media:
                            item["media"] = f'{media["codec"]} {media["width"]}x{media["height"]}'
                        elif now - MEDIA_ASKED.get(name, 0) > 60:
                            MEDIA_ASKED[name] = now
                            urllib.request.urlopen(f"{base}/control/query?what=media", timeout=2).read()
                    except Exception:
                        pass
            out[name] = item
        self._json(out)

    def _snapshot(self, cam):
        if cam.frame is None:
            self._send(503, b"no picture yet")
            return
        self._send(200, cam.frame, "image/jpeg")

    def _save_snapshot(self, cam):
        if cam.frame is None:
            return self._send(503, b"no picture yet")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        path = os.path.join(save_dir("snapshots"), f"{cam.name}-{stamp}.jpg")
        with open(path, "wb") as handle:
            handle.write(cam.frame)
        self._send(200, f"saved {win_path(path)}".encode())

    def _record(self, name):
        action = self._query().get("action", "toggle")
        rec = running_recorder(name)
        if rec and action in ("stop", "toggle"):
            rec.stop()
            return self._send(200, f"stopped, saved {win_path(rec.path)}".encode())
        if action == "stop":
            return self._send(200, b"not recording")
        rec, _new = start_recording(name, "manual")
        self._send(200, f"recording to {win_path(rec.path)} (press again to stop)".encode())

    def _ir_state(self, name):
        """Night vision as the camera reports it (iWFCam: cmd 0x13 sub 1, payload byte 8)."""
        base = CONTROLS[name]
        try:
            urllib.request.urlopen(f"{base}/control/query?what=ir", timeout=3).read()
            time.sleep(1.2)
            with urllib.request.urlopen(f"{base}/control/state", timeout=3) as r:
                reply = json.load(r)["last_by_cmd"]["19"]
            hexpart = reply.split()[-1]
            return self._json({"on": hexpart[48:50] == "01"})
        except Exception as error:
            return self._json({"error": str(error)}, 502)

    def _sleep_cfg(self, name):
        """Sleep settings as the camera reports them (iWFCam cmd 0x22 read)."""
        base = CONTROLS[name]
        try:
            urllib.request.urlopen(f"{base}/control/query?what=sleep", timeout=3).read()
            time.sleep(1.5)
            with urllib.request.urlopen(f"{base}/control/state", timeout=3) as r:
                cfg = json.load(r).get("sleep_config")
            return self._json(cfg or {"error": "camera did not answer"}, 200 if cfg else 502)
        except Exception as error:
            return self._json({"error": str(error)}, 502)

    def _listen(self, name):
        base = CONTROLS[name]
        try:
            urllib.request.urlopen(f"{base}/control/audio?on=1", timeout=3).read()
        except Exception:
            pass
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32k", "-analyzeduration", "0",
               "-f", "wav", "-i", f"{base}/audio.wav", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "48k",
               "-flush_packets", "1", "-f", "mp3", "pipe:1"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            proc.kill()

    def _ptz(self, name, what):
        q, s = self._query(), settings(name)["ptz"]
        try:
            if what == "move":
                d = q.get("dir")
                pan = {"left": -1, "right": 1}.get(d, 0) * PTZ_STEP["pan"] * float(q.get("speed", 1))
                tilt = {"up": 1, "down": -1}.get(d, 0) * PTZ_STEP["tilt"] * float(q.get("speed", 1))
                if not (pan or tilt):
                    return self._send(400, b"dir must be left/right/up/down")
                if s["invert_pan"]:
                    pan = -pan
                if s["invert_tilt"]:
                    tilt = -tilt
                return self._json(ptz_goto(name, s["x"] + pan, s["y"] + tilt))
            if what == "goto":
                return self._json(ptz_goto(name, float(q["x"]), float(q["y"])))
            if what == "go":
                p = next((p for p in s["presets"] if p["name"] == q.get("name")), None)
                if not p:
                    return self._send(404, b"no such position")
                return self._json(ptz_goto(name, p["x"], p["y"]))
            if what == "save":
                label = (q.get("name") or "").strip()[:30]
                if not label:
                    return self._send(400, b"name missing")
                s["presets"] = [p for p in s["presets"] if p["name"] != label] + [{"name": label, "x": s["x"], "y": s["y"]}]
                save_settings()
                return self._json(s)
            if what == "delete":
                s["presets"] = [p for p in s["presets"] if p["name"] != q.get("name")]
                save_settings()
                return self._json(s)
            if what == "invert":
                axis = "invert_pan" if q.get("axis") == "pan" else "invert_tilt"
                s[axis] = not s[axis]
                save_settings()
                return self._json(s)
            if what == "info":
                return self._json(s)
        except (KeyError, ValueError):
            return self._send(400, b"bad request")
        except Exception as error:  # bridge down
            return self._send(502, str(error).encode())
        self.send_error(404)

    def _settings(self, name):
        m = settings(name)["motion"]
        if self.command == "POST":
            body = self._body_json()
            if body is None:
                return self._send(400, b"JSON body expected")
            if "zones" in body:
                zones = []
                for z in body["zones"][:20]:
                    if isinstance(z, list) and len(z) == 4 and all(isinstance(v, (int, float)) for v in z):
                        zones.append([round(min(1, max(0, float(v))), 4) for v in z])
                m["zones"] = zones
                owner = m.get("zones_for")
                ptz = settings(name).get("ptz")
                if ptz is not None:
                    preset = next((p for p in ptz["presets"] if p["name"] == owner), None)
                    if preset:
                        preset["zones"] = zones
                    else:
                        m["free_zones"] = zones
            for key in ("on", "notify", "record", "ai"):
                if key in body:
                    m[key] = bool(body[key])
            if "skip_known" in body:
                m["skip_known"] = bool(body["skip_known"])
            if body.get("record_on") in ("motion", "person", "person_animal", "animal"):
                m["record_on"] = body["record_on"]
            if "sens" in body:
                m["sens"] = max(1, min(100, int(body["sens"])))
            if "brightness" in body or "contrast" in body:
                pic = settings(name).setdefault("picture", {"brightness": 0, "contrast": 0})
                for key in ("brightness", "contrast"):
                    if key in body:
                        pic[key] = max(-100, min(100, int(body[key])))
                CAMS[name].eq = (pic["brightness"], pic["contrast"])
                CAMS[name].restart_reader()
            save_settings()
        self._json(dict(m, record_on=m.get("record_on", "person"), ai=m.get("ai", True), skip_known=m.get("skip_known", True),
                        picture=settings(name).get("picture", {"brightness": 0, "contrast": 0})))

    def _saved_file(self, rest):
        rel = urllib.parse.unquote(rest)
        path = os.path.realpath(os.path.join(SAVE_ROOT, rel))
        if not path.startswith(os.path.realpath(SAVE_ROOT) + os.sep) or not os.path.isfile(path):
            return self.send_error(404)
        ctype = "image/jpeg" if path.endswith(".jpg") else "video/mp4" if path.endswith(".mp4") else "application/octet-stream"
        size = os.path.getsize(path)
        rng = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        with open(path, "rb") as handle:
            if rng:  # video seeking in the browser
                start = int(rng.group(1))
                end = min(int(rng.group(2)) if rng.group(2) else size - 1, size - 1)
                handle.seek(start)
                body = handle.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                body = handle.read()
                self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        if path == "":
            config = [{"name": n, "controls": n in CONTROLS, "features": sorted(FEATURES.get(n, ()))} for n in CAMS]
            with open(PAGE_FILE, encoding="utf-8") as handle:
                page = handle.read()
            body = page.replace("__CONFIG__", json.dumps(config)).replace("__SAVEROOT__", json.dumps(win_path(SAVE_ROOT)))
            self._send(200, body.encode(), "text/html; charset=utf-8")
            return
        if path == "state":
            return self._state()
        if path == "telemetry":
            with open(TELEMETRY_PAGE, encoding="utf-8") as handle:
                return self._send(200, handle.read().encode(), "text/html; charset=utf-8")
        if path == "telemetry/data":
            try:
                since = int(self._query().get("since", 0))
            except ValueError:
                since = 0
            return self._json(TELEMETRY.snapshot(since) if TELEMETRY else {"connected": False})
        if path == "telemetry/report":
            if TELEMETRY is None:
                return self._send(503, b"telemetry not running")
            period = "24h" if self._query().get("period") == "24h" else "today"
            def fetch(file):
                with urllib.request.urlopen(f"{CAMMEDIA_URL}/bigeye/file/{file}", timeout=8) as r:
                    return r.read()
            try:
                with urllib.request.urlopen(f"{CAMMEDIA_URL}/bigeye/list", timeout=8) as r:
                    pics = json.load(r)
            except Exception:
                pics = []
            if self._query().get("pictures") == "0":  # e.g. for sharing: numbers only, no pictures of the home
                pics = []
            page = TELEMETRY_MOD.build_report(TELEMETRY, period, fetch, pics)
            folder = os.path.join(SAVE_ROOT, "reports")
            os.makedirs(folder, exist_ok=True)
            target = os.path.join(folder, f"call-home-report-{period}-{datetime.datetime.now().strftime('%Y%m%d-%H%M')}.html")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(page)
            log(f"telemetry report saved {target}")
            return self._send(200, page.encode(), "text/html; charset=utf-8", {"X-Saved-To": win_path(target)})
        if path == "telemetry/uploads" or path.startswith("telemetry/upload/"):
            rest = "list" if path == "telemetry/uploads" else "file/" + path[len("telemetry/upload/"):]
            if rest != "list" and not re.match(r"^file/\d{4}-\d{2}-\d{2}/[A-Za-z0-9._-]+$", rest):
                return self.send_error(404)
            try:
                with urllib.request.urlopen(f"{CAMMEDIA_URL}/bigeye/{rest}", timeout=8) as reply:
                    return self._send(200, reply.read(), reply.headers.get("Content-Type", "application/octet-stream"))
            except Exception as error:
                return self._send(502, str(error).encode())
        if path == "events":
            return self._json(list(EVENTS)[-40:][::-1])
        if path == "addons":
            return self._json(ADDONS)
        if path.startswith("saved/"):
            return self._saved_file(self.path.split("?")[0][len("/saved/"):])
        if path.startswith("open/") and path[5:] in FOLDERS:
            folder = os.path.join(SAVE_ROOT, FOLDERS[path[5:]])
            os.makedirs(folder, exist_ok=True)
            subprocess.Popen([CFG["windows"]["explorer"], win_path(folder)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._send(200, f"opened {win_path(folder)}".encode())
        if path.startswith("frigate/"):
            return self._frigate(self.path.lstrip("/")[len("frigate/"):])
        if path.endswith(".jpg") and path[:-4] in CAMS:
            return self._snapshot(CAMS[path[:-4]])
        if path.startswith("reconnect/"):
            which = path[len("reconnect/"):]
            if self.command != "POST":
                return self.send_error(405)
            if which == "all":
                return self._send(200, reconnect([n for n in CAMS], only_running=True).encode())
            if which in CAMS:
                return self._send(200, reconnect([which]).encode())
            return self.send_error(404)
        if path == "pets":  # names for the household's animals: {"cat": "Whiskers", "dog": ""}
            pets = SETTINGS.setdefault("_page", {}).setdefault("pets", {})
            if self.command == "POST":
                body = self._body_json() or {}
                for k in ("cat", "dog"):
                    if isinstance(body.get(k), str):
                        pets[k] = body[k].strip()[:30]
                save_settings()
            return self._json(pets)
        if path == "faces" or path.startswith("faces/"):
            parts = path.split("/")
            q = self._query()
            try:
                if len(parts) == 3 and parts[1] == "enroll" and parts[2] in CAMS and self.command == "POST":
                    frame = CAMS[parts[2]].frame
                    if frame is None:
                        return self._json({"error": "no picture"}, 503)
                    req = urllib.request.Request(f"http://127.0.0.1:8771/enroll?name={urllib.parse.quote(q.get('name', ''))}",
                                                 data=frame, headers={"Content-Type": "image/jpeg"})
                elif len(parts) == 2 and parts[1] == "delete" and self.command == "POST":
                    req = f"http://127.0.0.1:8771/faces/delete?name={urllib.parse.quote(q.get('name', ''))}"
                elif len(parts) == 1:
                    req = "http://127.0.0.1:8771/faces"
                else:
                    return self.send_error(404)
                with urllib.request.urlopen(req, timeout=20) as reply:
                    return self._json(json.load(reply))
            except Exception as error:
                return self._json({"error": f"AI service: {error}"}, 502)
        if path.startswith("addon/"):
            parts = path.split("/")
            fn = ADDON_ACTIONS.get(tuple(parts[1:3])) if len(parts) == 3 else None
            if fn is None or self.command != "POST":
                return self.send_error(404)
            return self._json(fn())
        if path.startswith("follow/") and "ptz" in FEATURES.get(path[7:], ()):
            c = settings(path[7:]).setdefault("follow", {"mode": "off", "home": "", "return_after": 60})
            if self.command == "POST":
                body = self._body_json() or {}
                if body.get("mode") in ("off", "person", "person_animal"):
                    c["mode"] = body["mode"]
                if isinstance(body.get("home"), str):
                    c["home"] = body["home"][:30]
                if isinstance(body.get("return_after"), int):
                    c["return_after"] = max(10, min(3600, body["return_after"]))
                save_settings()
            return self._json(c)
        if path == "home":
            page = SETTINGS.setdefault("_page", {})
            if self.command == "POST":
                page["home"] = not page.get("home", False)
                save_settings()
                log("home mode " + ("on (sensor paused)" if page["home"] else "off (sensor armed)"))
            return self._json({"home": page.get("home", False)})
        if path == "autofix":
            page = SETTINGS.setdefault("_page", {})
            if self.command == "POST":
                page["autofix"] = not page.get("autofix", True)
                save_settings()
            return self._json({"on": page.get("autofix", True), "last": {k: round(v) for k, v in LAST_FIX.items()}})
        if path.startswith("frame/") and path[6:] in CAMS:
            # Next picture newer than ?after=: the page asks for one picture at a time,
            # so no connection stays open (browsers allow only 6 per server, across all tabs).
            cam = CAMS[path[6:]]
            try:
                after = float(self._query().get("after", 0))
            except ValueError:
                after = 0.0
            frame, stamp = cam.wait_frame(after, timeout=8)
            if frame is None or stamp <= after:
                return self._send(204, b"")
            return self._send(200, frame, "image/jpeg", {"X-Stamp": repr(stamp)})
        if path.startswith("snap/") and path[5:] in CAMS:
            return self._save_snapshot(CAMS[path[5:]])
        if path.startswith("rec/") and path[4:] in CAMS:
            return self._record(path[4:])
        if path.startswith("settings/") and path[9:] in CAMS:
            return self._settings(path[9:])
        if path.startswith("sleepcfg/") and path[9:] in CONTROLS:
            return self._sleep_cfg(path[9:])
        if path.startswith("irstate/") and path[8:] in CONTROLS:
            return self._ir_state(path[8:])
        if path.startswith("ptz/"):
            parts = path.split("/")
            if len(parts) == 3 and "ptz" in FEATURES.get(parts[1], ()):
                return self._ptz(parts[1], parts[2])
            return self.send_error(404)
        if path.startswith("listen/") and path[7:] in CONTROLS and "listen" in FEATURES.get(path[7:], ()):
            return self._listen(path[7:])
        if path.startswith("ctl/"):
            parts = path.split("/", 2)
            base = CONTROLS.get(parts[1]) if len(parts) == 3 else None
            if base is None or parts[2] not in ALLOWED_CONTROL_PATHS:
                self.send_error(404)
                return
            if (parts[2] in POST_ONLY_PATHS) != (self.command == "POST"):
                self.send_error(405)
                return
            query = urllib.parse.urlsplit(self.path).query
            req = urllib.request.Request(f"{base}/{parts[2]}?{query}",
                                         data=b"" if self.command == "POST" else None, method=self.command)
            try:
                with urllib.request.urlopen(req, timeout=4) as reply:
                    body, code = reply.read(), reply.status
            except Exception as error:  # bridge down or camera asleep
                body, code = str(error).encode(), 502
            self._send(code, body)
            return
        name = path.rsplit(".", 1)[0]
        cam = CAMS.get(name)
        if cam is None or not path.endswith(".mjpg"):
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = 0.0
        try:
            while True:
                frame, stamp = cam.wait_frame(last)
                if frame is None or stamp <= last:
                    continue
                last = stamp
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                 + str(len(frame)).encode() + b"\r\n\r\n" + frame + b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--fps", type=int, default=0, help="cap on pictures per second; 0 = the camera's own rate")
    ap.add_argument("--quality", type=int, default=2, help="JPEG quality, 2 (best) to 31")
    ap.add_argument("--control", action="append", default=[], metavar="NAME=URL",
                    help="bridge base URL for a camera's own controls, e.g. door=http://127.0.0.1:18765; "
                         "add ,listen ,pir and/or ,ptz to enable those buttons")
    ap.add_argument("--frigate", help="Frigate API base URL, e.g. http://127.0.0.1:15000")
    ap.add_argument("--cammedia", help="(no longer shown on the page; accepted so old service files still start)")
    ap.add_argument("cams", nargs="+", help="name=url[,flip]")
    args = ap.parse_args()
    for item in args.cams:
        name, url = item.split("=", 1)
        flip = url.endswith(",flip")
        if flip:
            url = url[:-len(",flip")]
        CAMS[name] = Camera(name, url, args.fps, args.quality, flip)
    global FRIGATE
    FRIGATE = args.frigate.rstrip("/") if args.frigate else None
    for item in args.control:
        name, rest = item.split("=", 1)
        url, *opts = rest.split(",")
        CONTROLS[name] = url.rstrip("/")
        FEATURES[name] = set(opts)
    os.makedirs(SAVE_ROOT, exist_ok=True)
    load_settings()
    for name, cam in CAMS.items():
        pic = settings(name).get("picture")
        if pic:
            cam.eq = (pic.get("brightness", 0), pic.get("contrast", 0))
    load_events()
    ADDONS.extend(load_addons())
    global TELEMETRY
    try:
        import importlib.util as _iu
        spec = _iu.spec_from_file_location("bigeye_telemetry", os.path.join(HERE, "telemetry.py"))
        mod = _iu.module_from_spec(spec)
        spec.loader.exec_module(mod)
        TELEMETRY = mod.Telemetry(log, SSH_ROUTES, CFG["camera_machine"]["logs"],
                                  {c["ip"]: n for n, c in CFG["cameras"].items() if c.get("ip")})
        globals()["TELEMETRY_MOD"] = mod
    except Exception as error:
        log(f"telemetry failed to start: {error}")
    log("page started")
    threading.Thread(target=picture_monitor, daemon=True).start()
    threading.Thread(target=autofix_loop, daemon=True).start()
    print(f"Sentinel-BigEye: http://localhost:{args.port}/")
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
