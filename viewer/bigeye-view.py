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
import shutil
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
from bigeye_config import CFG, camera as cfg_camera, path as cfg_path, h264_args  # noqa: E402  (one TOML file, see bigeye_config.py)
PREROLL_SECONDS = 4  # pictures kept in memory so a motion recording can start before the alarm


class Camera:
    def __init__(self, name, url, fps, quality, flip=False):
        self.name, self.url, self.fps, self.quality, self.flip = name, url, fps, quality, flip
        self.process = False  # set before the reader starts: send an MJPEG camera through ffmpeg too (clock, picture help)
        self.frame = None
        self.stamp = 0.0
        self.recent = collections.deque()  # (stamp, jpeg) for the last PREROLL_SECONDS
        self.cond = threading.Condition()
        self.proc = None
        self.eq = None  # (brightness -100..100, contrast -100..100) done here, for cameras without their own
        self.enhance = {}  # "deflicker": bool, "denoise": bool, "night_boost": 0..2 (BigEye picture help)
        self.privacy = []  # [[x0, y0, x1, y1], ...] as 0..1: blacked out in the live picture, recordings and AI
        self.count = 0     # pictures received (for frame rate)
        self.osd = False   # draw our own date/time stamp (the door camera lost its own)
        threading.Thread(target=self._run, daemon=True).start()

    def restart_reader(self):
        """Drop the local reader; it reconnects by itself two seconds later."""
        if self.proc and self.proc.poll() is None:
            self.proc.kill()

    def _publish(self, frame):
        now = time.time()
        with self.cond:
            self.frame, self.stamp = frame, now
            self.count += 1
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

    def passthrough(self):
        return self.url.startswith("http") and not self.flip and not self.url.endswith(".h265") and not self.process

    def _run(self):
        time.sleep(0.2)  # let main() set .process first
        if self.passthrough():
            return self._run_passthrough()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if self.url.startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp"]
        if self.url.endswith(".h265"):  # raw H.265 from the 360 bridge: no container, no timestamps
            cmd += ["-use_wallclock_as_timestamps", "1", "-f", "hevc"]
        elif self.url.startswith("http"):  # MJPEG over HTTP (window camera): no timestamps either
            cmd += ["-use_wallclock_as_timestamps", "1"]
        cmd += ["-fflags", "nobuffer", "-flags", "low_delay", "-i", self.url, "-an", "-q:v", str(self.quality)]
        # Every frame the camera sends, at its own rate (--fps 0), or capped at --fps.
        cmd += ["-fps_mode", "vfr"] if not self.fps else ["-r", str(self.fps)]
        while True:
            filters = ["hflip,vflip"] if self.flip else []  # camera mounted upside down (ceiling)
            o = getattr(self, "orient", None) or {}   # turn / mirror chosen on the page (phones on their side etc.)
            turn = {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}.get(int(o.get("rotate", 0)))
            if turn:
                filters.append(turn)
            if o.get("mirror"):
                filters.append("hflip")
            # The door camera switches between full and limited colour range; the JPEG
            # encoder refuses limited range ("Non full-range YUV"), so always convert.
            filters.append("scale=out_range=pc,format=yuvj420p")
            if self.eq and any(self.eq):
                filters.append(f"eq=brightness={self.eq[0] / 200:.3f}:contrast={1 + self.eq[1] / 100:.3f}")
            polys = []
            for z in self.privacy or []:
                if isinstance(z, dict) and z.get("poly"):
                    polys.append(z["poly"])
                    continue
                x0, y0, x1, y1 = (max(0.0, min(1.0, float(v))) for v in z)
                filters.append(f"drawbox=x=iw*{min(x0, x1):.4f}:y=ih*{min(y0, y1):.4f}:w=iw*{abs(x1 - x0):.4f}:h=ih*{abs(y1 - y0):.4f}:color=black@1:t=fill")
            e = self.enhance or {}
            box = clock_box(self.name) if e.get("hide_clock") else None
            if box:  # blur the camera's own clock (works at any resolution: all in fractions of the picture)
                x, y, w, h = box
                filters.append(f"split[ck0][ck1];[ck1]crop=iw*{w}:ih*{h}:iw*{x}:ih*{y},"
                               f"boxblur=lr='min(w\\,h)/3':lp=2:cr='min(cw\\,ch)/3':cp=2[ck2];[ck0][ck2]overlay=W*{x}:H*{y}")
            if e.get("deflicker"):  # smooths the brightness pulsing of lamps on mains power (50 Hz)
                filters.append("deflicker=mode=pm:size=5")
            if e.get("denoise"):  # atadenoise (LGPL): light, and smears movement less than hqdn3d (GPL) did
                filters.append("atadenoise=0a=0.02:1a=0.04:2a=0.02:0b=0.04:1b=0.08:2b=0.04:s=5")
            if e.get("night_boost"):  # lift dark areas without touching the camera's night vision
                filters.append(["", "eq=gamma=1.35:brightness=0.03:saturation=1.1", "eq=gamma=1.7:brightness=0.06:saturation=1.15"][int(e["night_boost"])])
            post = []
            if self.osd:
                post.append("drawtext=fontfile=" + OSD_FONT + ":text='%{localtime\\:%Y-%m-%d %H\\\\\\:%M\\\\\\:%S}  "
                            + cam_label(self.name).replace("'", "").replace(":", "").replace("\\", "") + "':x=w-tw-16:y=16:fontsize=h/32:fontcolor=white:borderw=2:bordercolor=black@0.8")
            if polys:  # free-hand privacy masks: a picture with black shapes laid over the video
                mask = privacy_mask_png(self.name, polys)
                graph = (",".join(filters) + "[pre];" if filters else "[in]null[pre];") + \
                    f"movie={mask}[m];[m][pre]scale2ref[ms][v];[v][ms]overlay" + ("," + ",".join(post) if post else "")
                full = cmd + ["-vf", graph, "-f", "mjpeg", "pipe:1"]
            else:
                chain = filters + post
                full = cmd + (["-vf", ",".join(chain)] if chain else []) + ["-f", "mjpeg", "pipe:1"]
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
OSD_FONT = next((f for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
                 if os.path.exists(f)), "")


def clean_shape(z):
    """A zone or mask: [x0, y0, x1, y1] or {"poly": [[x, y], ...]} (free-hand), all 0..1."""
    if isinstance(z, list) and len(z) == 4 and all(isinstance(v, (int, float)) for v in z):
        return [round(min(1, max(0, float(v))), 4) for v in z]
    if isinstance(z, dict) and isinstance(z.get("poly"), list):
        pts = [[round(min(1, max(0, float(p[0]))), 4), round(min(1, max(0, float(p[1]))), 4)]
               for p in z["poly"][:300] if isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) for v in p)]
        return {"poly": pts} if len(pts) >= 3 else None
    return None


def privacy_mask_png(name, polys):
    """Black free-hand shapes on a transparent 1920x1080 picture, for ffmpeg to lay over the video."""
    from PIL import Image, ImageDraw
    folder = os.path.join(os.path.expanduser("~/.config/sentinel-bigeye"), "masks")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{name}.png")
    im = Image.new("RGBA", (1920, 1080), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    for poly in polys:
        pts = [(max(0, min(1, float(x))) * 1919, max(0, min(1, float(y))) * 1079) for x, y in poly]
        if len(pts) >= 3:
            d.polygon(pts, fill=(0, 0, 0, 255))
    im.save(path)
    return path


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
                         "control/sleepflags", "calibrate")
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
# Where a camera burns its own clock into the picture (fractions x, y, w, h), so BigEye can blur it away and show its
# own clock top-left instead. bigeye.toml [cameras.<name>] clock_box = [x, y, w, h] overrides.
CLOCK_BOX = {"cam360": (0.735, 0.03, 0.26, 0.075)}


def cam_label(name):
    """The name the owner gave the camera on the page (settings keep the internal name, so nothing is lost)."""
    return (SETTINGS.get(name, {}) or {}).get("label") or name.capitalize()


def clock_box(name):
    box = cfg_camera(name, "clock_box", None)
    return tuple(box) if box else CLOCK_BOX.get(name)
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
               "-i", "pipe:0", "-vf", f"scale={width}:{height}", *h264_args(23), "-movflags", "+faststart", self.path]
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
MANUAL_UNTIL = {}  # camera name -> until when the owner is steering by hand (BigEye follow keeps still)
MANUAL_PAUSE = 90
PTZ_STEP = {"pan": 0.025, "tilt": 0.04}


# ---- favourites that fix themselves: a reference picture per favourite --------------------
REF_DIR = os.path.join(os.path.dirname(SETTINGS_FILE), "presets")
AUTOFIX_STATE = {}  # camera -> {"running": bool, "last": report, "restart_seen": time}


def _grey(jpeg, w=480, h=270):
    import numpy as np
    from PIL import Image
    img = Image.open(io.BytesIO(jpeg)); img.draft("L", (w, h))
    a = np.asarray(img.convert("L").resize((w, h)), np.float32)
    a[: h // 9, int(w * 0.6):] = a.mean()  # the camera's clock changes every second
    return a - a.mean()


def _shift(a, b):
    """How far the picture content moved from a (reference) to b (now): share of width/height, and how sure."""
    import numpy as np
    f = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    r = np.fft.ifft2(f / (np.abs(f) + 1e-6)).real
    y, x = np.unravel_index(r.argmax(), r.shape)
    sure = float(r.max() / (r.std() + 1e-9))
    if y > r.shape[0] // 2:
        y -= r.shape[0]
    if x > r.shape[1] // 2:
        x -= r.shape[1]
    return -x / r.shape[1], -y / r.shape[0], sure


def ref_path(name, preset):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", preset)
    return os.path.join(REF_DIR, name, safe + ".jpg")


def save_reference(name, preset):
    frame = CAMS[name].frame
    if frame:
        os.makedirs(os.path.dirname(ref_path(name, preset)), exist_ok=True)
        with open(ref_path(name, preset), "wb") as handle:
            handle.write(frame)


def _frame_after(name, seconds):
    time.sleep(seconds)
    cam = CAMS[name]
    if time.time() - cam.stamp > 2:
        raise RuntimeError("the picture is not live")
    return cam.frame


def autofix_positions(name):
    """Go to favourites with a reference picture, find where the picture really is, shift all favourites."""
    st = AUTOFIX_STATE.setdefault(name, {})
    if st.get("running"):
        return {"error": "already running"}
    st["running"] = True
    try:
        s = settings(name)["ptz"]
        fol = settings(name).get("follow", {})
        gp, gt = fol.get("gain_pan", -4.0), fol.get("gain_tilt", -2.185)
        refs = [p for p in s["presets"] if os.path.exists(ref_path(name, p["name"]))]
        if not refs:
            return {"error": "no favourite has a reference picture yet: save or re-save a favourite first"}
        best = None
        for p in refs[:3]:
            ref = _grey(open(ref_path(name, p["name"]), "rb").read())
            x, y = p["x"], p["y"]
            # Coarse search first when the picture does not match at all (big shift after a restart).
            tries = [(0, 0), (0, 0.1), (0, -0.1), (0, 0.2), (0, -0.2), (0.12, 0), (-0.12, 0), (0.12, 0.15), (-0.12, 0.15)]
            found = None
            for dx, dy in tries:
                tx, ty = min(1, max(0, x + dx)), min(1, max(0, y + dy))
                ptz_goto(name, tx, ty, quiet=8)
                sx, sy, sure = _shift(ref, _grey(_frame_after(name, 4)))
                if sure > 7 and abs(sx) < 0.45 and abs(sy) < 0.45:
                    found = (tx, ty, sx, sy, sure)
                    break
            if not found:
                continue
            tx, ty, sx, sy, sure = found
            for _ in range(3):  # refine: move by the measured shift
                if abs(sx) < 0.02 and abs(sy) < 0.02:
                    break
                tx = min(1, max(0, tx - sx / gp * 0.9)); ty = min(1, max(0, ty - sy / gt * 0.9))
                ptz_goto(name, tx, ty, quiet=8)
                sx, sy, sure = _shift(ref, _grey(_frame_after(name, 3.5)))
            if sure > 7 and (best is None or sure > best[4]):
                best = (p, tx, ty, (abs(sx) + abs(sy)), sure)
            if best and best[3] < 0.03:
                break
        if not best:
            report = {"error": "could not recognise any favourite's picture (too dark, or the view changed a lot)"}
        else:
            p, tx, ty, err, sure = best
            dx, dy = tx - p["x"], ty - p["y"]
            for q in s["presets"]:
                q["x"] = round(min(1.0, max(0.0, q["x"] + dx)), 4); q["y"] = round(min(1.0, max(0.0, q["y"] + dy)), 4)
            save_settings()
            ptz_goto(name, p["x"], p["y"])
            report = {"ok": True, "from": p["name"], "shift": {"x": round(dx, 4), "y": round(dy, 4)}, "sure": round(sure)}
        st["last"] = report
        st["restart_seen"] = 0
        log(f"{name}: auto-fix positions {report}")
        return report
    except RuntimeError as error:
        return {"error": str(error)}
    finally:
        st["running"] = False


def watch_restarts():
    """The 360 sends its new live-video password at every start: that is how a restart shows up."""
    seen = 0
    while True:
        time.sleep(15)
        if not TELEMETRY:
            continue
        with TELEMETRY.lock:
            new = [e for e in TELEMETRY.events if e["cat"] == "password" and e["t"] > max(seen, PAGE_STARTED)]
        if new:
            seen = max(e["t"] for e in new)
            for name in CAMS:
                if "ptz" in FEATURES.get(name, ()):
                    AUTOFIX_STATE.setdefault(name, {})["restart_seen"] = seen
                    log(f"{name}: camera restart seen at {time.strftime('%H:%M', time.localtime(seen))}; favourites may be off")


def lock_enforce(name):
    """Locked: the camera's own tracking must stay off (it may switch it on again after a restart)."""
    try:
        urllib.request.urlopen(f"{CONTROLS[name]}/tracking?on=0", timeout=4).read()
    except Exception:
        pass


def lock_loop():
    while True:
        time.sleep(60)
        for name in CAMS:
            if settings(name).get("ptz", {}).get("locked"):
                lock_enforce(name)


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
    cfg = settings(name).get("motion", {})
    if SETTINGS.get("_page", {}).get("home") and not cfg.get("at_home"):
        return  # Home mode: the owner is in; nothing is recorded or sent (unless this camera keeps recording at home)
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
    if is_real_alarm(name, info) and offsite_dir():
        threading.Thread(target=offsite_copy, args=(name, frame, "stranger"), daemon=True).start()
    if cfg.get("notify") and now - LAST_NOTIFY.get(name, 0) > NOTIFY_GAP:
        LAST_NOTIFY[name] = now
        who = (info.get('name') or ('Stranger' if info.get('label') == 'person' else info.get('label')) or 'movement').capitalize()
        push_alert(f"{who} at the {name} camera", f"{who} at {stamp.strftime('%H:%M:%S')} (you are Away).", "urgent", name,
                   os.path.relpath(pic, SAVE_ROOT))
        threading.Thread(target=notify_phone, args=(name, f"{(info.get('name') or ('Stranger' if info.get('label') == 'person' else info.get('label')) or 'movement').capitalize()} at {stamp.strftime('%H:%M:%S')}"),
                         daemon=True).start()


# ── off-site copy of real alarms ───────────────────────────────────────────
# A real alarm: a stranger (a person who is not a known face) while Away, or at night on a camera that keeps recording
# in Home mode (door, window). Its pictures go straight into a cloud-synced folder (OneDrive), one a second, so they
# are online within seconds even if the PCs are taken or the power is cut; the finished clip follows.
OFFSITE_ACTIVE = set()
OFFSITE_MAX_S = 300
ALARM_GRACE_S = 20   # coming home: wait this long for your phone or face before calling it an alarm


def offsite_dir():
    d = (CFG.get("alarm") or {}).get("offsite_dir")
    return os.path.expanduser(d) if d else None


def is_night(now=None):
    try:
        a, b = (int(x) for x in str((CFG.get("alarm") or {}).get("night", "22-07")).split("-"))
    except ValueError:
        a, b = 22, 7
    h = datetime.datetime.fromtimestamp(now or time.time()).hour
    return h >= a or h < b if a > b else a <= h < b


def is_real_alarm(name, info):
    if info.get("label") != "person" or info.get("name"):
        return False   # only strangers: known faces and animals are never an alarm
    away = not SETTINGS.get("_page", {}).get("home")
    cfg = settings(name).get("motion", {})
    return away or (cfg.get("at_home") and is_night())


def offsite_copy(name, first_frame, event_name):
    """Write the alarm's pictures (the 3 s before, then one a second) and finally its clip into the off-site folder."""
    root = offsite_dir()
    if not root or name in OFFSITE_ACTIVE:
        return
    OFFSITE_ACTIVE.add(name)
    try:
        before = list(CAMS[name].recent)         # keep what happened just before, while we wait
        stamp = datetime.datetime.now()
        if event_name != "TEST":
            for _ in range(ALARM_GRACE_S):
                time.sleep(1)
                if SETTINGS.get("_page", {}).get("home") and not is_night():
                    log(f"{name}: not an alarm (Home mode came on: your phone or face)")
                    return
                found = (ADDON_STATUS.get(name, {}).get("ai") or {}).get("found") or []
                if any(d.get("name") and not d.get("pet") for d in found):
                    log(f"{name}: not an alarm (a known face)")
                    return
        folder = os.path.join(root, stamp.strftime("%Y-%m-%d"), f"{stamp.strftime('%H%M%S')}-{name}-{event_name}")
        os.makedirs(folder, exist_ok=True)
        cam = CAMS[name]
        n = 0
        for t, jpeg in before:  # what happened just before
            with open(os.path.join(folder, f"{name}-{datetime.datetime.fromtimestamp(t).strftime('%H%M%S-%f')[:-3]}-before.jpg"), "wb") as fh:
                fh.write(jpeg)
        if first_frame:
            with open(os.path.join(folder, f"{name}-{stamp.strftime('%H%M%S')}-ALARM.jpg"), "wb") as fh:
                fh.write(first_frame)
        log(f"{name}: alarm, copying pictures off-site to {folder}")
        start, last_stamp, clip = time.time(), None, None
        while time.time() - start < OFFSITE_MAX_S:
            rec = running_recorder(name)
            if rec:
                clip = rec.path
            elif time.time() - start > 15:
                break                       # the recording has ended
            if cam.frame is not None and cam.stamp != last_stamp:
                last_stamp = cam.stamp
                n += 1
                with open(os.path.join(folder, f"{name}-{datetime.datetime.now().strftime('%H%M%S')}.jpg"), "wb") as fh:
                    fh.write(cam.frame)
            time.sleep(1)
        if clip:
            for _ in range(30):             # the recorder finishes the file a moment after it stops
                if not running_recorder(name):
                    break
                time.sleep(1)
            try:
                shutil.copy2(clip, os.path.join(folder, os.path.basename(clip)))
            except OSError as error:
                log(f"{name}: off-site clip copy failed: {error}")
        log(f"{name}: alarm off-site copy done ({n} pictures{', clip' if clip else ''})")
    except Exception as error:
        log(f"{name}: off-site copy failed: {error}")
    finally:
        OFFSITE_ACTIVE.discard(name)


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
    def manual(name):
        return time.time() < MANUAL_UNTIL.get(name, 0)

    @staticmethod
    def has_control(name, kind):
        if name not in CONTROLS:
            return False
        return kind != "ir" or "ptz" not in FEATURES.get(name, ())  # night vision on/off: iWFCam cameras

    @staticmethod
    def control(name, path):
        with urllib.request.urlopen(f"{CONTROLS[name]}/{path}", timeout=4) as r:
            return r.read()

    @staticmethod
    def set_quality(name, level):
        if name not in CONTROLS:
            return
        if "ptz" in FEATURES.get(name, ()):
            path = "quality?q=" + ("fhd" if level == "high" else "hd")
        else:
            path = "control/resolution?size=" + ("1920x1080" if level == "high" else "640x360")
        urllib.request.urlopen(f"{CONTROLS[name]}/{path}", timeout=4).read()

    @staticmethod
    def notify(title, text):
        push_alert(title, text, "action")
        if NTFY and os.path.exists(NTFY_TOPIC_FILE):
            notify_phone(title, text)

    @staticmethod
    def page_settings():
        return SETTINGS.setdefault("_page", {})

    @staticmethod
    def phones_home():
        return phones_home()

    @staticmethod
    def set_home(on, reason):
        page = SETTINGS.setdefault("_page", {})
        if page.get("home", False) != on:
            page["home"] = on
            save_settings()
            log(f"home mode {'on' if on else 'off'} ({reason})")

    @staticmethod
    def events():
        return list(EVENTS)

    @staticmethod
    def save_root():
        return SAVE_ROOT

    @staticmethod
    def locked(name):
        return bool(settings(name).get("ptz", {}).get("locked"))

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
            ADDON_MODULES[file[:-3]] = module
        except Exception as error:
            log(f"add-on {file} failed to start: {error}")
    log(f"add-ons: {', '.join(loaded) or 'none'}")
    return loaded


ADDONS = []
ADDON_MODULES = {}  # add-on name -> its module (e.g. the narrator's write_text for the daily story)


def day_story(date):
    """A short written story of one day's events, by the local language model."""
    narr = ADDON_MODULES.get("narrator")
    if not narr:
        return {"error": "the narrator add-on is not loaded"}
    day = [e for e in EVENTS if time.strftime("%Y-%m-%d", time.localtime(e["time"])) == date]
    lines = []
    for e in day:
        desc = narr.DESCRIPTIONS.get(e.get("picture") or "", {}).get("desc") or ""
        lines.append(f"{time.strftime('%H:%M', time.localtime(e['time']))} {e['cam']}: {e.get('label') or 'movement'}. {desc}")
    calls = ""
    if TELEMETRY:
        snap = TELEMETRY.snapshot(10**12)
        if date == time.strftime("%Y-%m-%d"):
            calls = f"The cameras tried to call their vendor clouds {snap['today']:,} times today; none of it left the house."
    if not lines:
        return {"date": date, "text": "Nothing was recorded on " + date + ". " + calls, "events": 0}
    prompt = ("You write the daily summary for a home camera system, for the owner. Plain English, friendly, short: "
              "a title line, then 5-10 sentences in time order, grouping repeats (for example 'the cat came by three times "
              "in the afternoon'). Mention anything unusual first if there is something. Do not invent anything that is not in the list.\n\n"
              f"Date: {date}. Events (time, camera, what the detector saw, description):\n" + "\n".join(lines[-80:]) +
              ("\n\n" + calls if calls else ""))
    try:
        text = narr.write_text(prompt)
    except Exception as error:
        return {"error": f"language model not answering: {error}"}
    folder = os.path.join(SAVE_ROOT, "reports")
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, f"story-{date}.md"), "w", encoding="utf-8") as handle:
        handle.write(text + "\n")
    return {"date": date, "text": text, "events": len(day)}


def story_at_night():
    """Write the day's story at 22:00 and send it to the phone (if phone messages are set up)."""
    done = ""
    while True:
        time.sleep(60)
        today = time.strftime("%Y-%m-%d")
        if time.strftime("%H:%M") >= "22:00" and done != today:
            done = today
            r = day_story(today)
            log(f"daily story: {r.get('error') or str(r.get('events')) + ' events'}")
            if r.get("text") and NTFY and os.path.exists(NTFY_TOPIC_FILE):
                notify_phone("today", r["text"][:3500])


TELEMETRY = None  # live call-home monitor (telemetry.py), started in main()
TELEMETRY_PAGE = os.path.join(HERE, "telemetry.html")
CAMMEDIA_URL = CFG["camera_machine"]["stand_in_media_url"].rstrip("/")  # the 360 stand-in cloud's caught uploads

# ---------------------------------------------------------------- phone app: alerts, pairing, devices
# The Android app (bigeye-android) shows this same page and keeps a long-poll on /app/alerts for the few
# alerts that need the owner: a person while Away, a camera gone for 10 minutes, the guard asking for help.
# The phone reaches BigEye through a second listener (--app-port) that only answers paired devices.
import hashlib  # noqa: E402
import hmac  # noqa: E402
import secrets  # noqa: E402

ALERTS = collections.deque(maxlen=200)
ALERT_SEQ = [int(time.time() * 10)]  # keeps growing across restarts, so the phone never misses one
ALERT_COND = threading.Condition()
APP_DIR = os.path.expanduser("~/.config/sentinel-bigeye")
DEVICES_FILE = os.path.join(APP_DIR, "app-devices.json")   # only key hashes are stored
APK_FILE = os.path.join(APP_DIR, "android", "sentinel-bigeye.apk")
PAIR_CODE = {"code": None, "until": 0}
DEVICES_LOCK = threading.Lock()


def push_alert(title, text, level="action", cam=None, picture=None):
    """level: 'urgent' (a person while Away) or 'action' (something needs the owner)."""
    with ALERT_COND:
        ALERT_SEQ[0] += 1
        ALERTS.append({"id": ALERT_SEQ[0], "t": round(time.time()), "title": title, "text": text,
                       "level": level, "cam": cam, "picture": picture})
        ALERT_COND.notify_all()


def load_devices():
    try:
        with open(DEVICES_FILE, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return []


def save_devices(devices):
    os.makedirs(APP_DIR, exist_ok=True)
    tmp = DEVICES_FILE + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        json.dump(devices, handle, indent=1)
    os.replace(tmp, DEVICES_FILE)


# ── phone presence: the BigEye app says which Wi-Fi addresses it has on every poll ──────────
# A phone with an address in the home network (bigeye.toml [app] home_prefix, e.g. "192.168.1.") is home.
PRESENCE = {}   # device name -> {"t": time, "home": bool}
PRESENCE_FRESH_S = 150


def note_presence(device, wifi):
    prefix = (CFG.get("app") or {}).get("home_prefix") or ""
    if not device or wifi is None or not prefix:
        return
    addrs = [a.strip() for a in wifi.split(",") if a.strip()]
    PRESENCE[device] = {"t": time.time(), "home": any(a.startswith(prefix) for a in addrs)}


def phones_home():
    """True: a paired phone is on the home Wi-Fi now; False: phones report, none is home; None: no phone reports."""
    fresh = [p for p in PRESENCE.values() if time.time() - p["t"] < PRESENCE_FRESH_S]
    if not fresh:
        return None
    return any(p["home"] for p in fresh)


def device_for_key(key):
    if not key or len(key) < 20:
        return None
    digest = hashlib.sha256(key.encode()).hexdigest()
    with DEVICES_LOCK:
        devices = load_devices()
        for d in devices:
            if hmac.compare_digest(d["key_sha256"], digest):
                if time.time() - d.get("last_seen", 0) > 300:
                    d["last_seen"] = round(time.time())
                    save_devices(devices)
                return d
    return None


def camera_watch():
    """A camera that was live and has been gone for 10 minutes is worth an alert (once, until it is back)."""
    since, ever, told = {}, set(), set()
    battery_log = os.path.expanduser("~/.cache/sentinel-bigeye-battery.csv")  # to see if a camera's power sags before it hangs
    last_battery = 0
    while True:
        now = time.time()
        if now - last_battery >= 60:
            last_battery = now
            for name in [n for n in CAMS if "pir" in FEATURES.get(n, ())]:  # battery cameras (window, W2)
                try:
                    with urllib.request.urlopen(f"{CONTROLS[name]}/status", timeout=2) as r:
                        st = json.load(r)
                    age = round(now - CAMS[name].stamp, 1) if CAMS[name].stamp else -1
                    new = not os.path.exists(battery_log)
                    with open(battery_log, "a", encoding="utf-8") as handle:
                        if new:
                            handle.write("time,camera,battery_pct,battery_mv,charging,picture_age_s\n")
                        handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{name},{st.get('battery_pct')},{st.get('battery_mv')},"
                                     f"{int(bool(st.get('charging')))},{age}\n")
                except Exception:
                    pass
        for name, cam in CAMS.items():
            live = cam.stamp and now - cam.stamp < 15
            if live:
                ever.add(name)
                since.pop(name, None)
                if name in told:
                    told.discard(name)
                    push_alert(f"{name} camera is back", f"The {name} camera has a picture again.", "info", name)
                continue
            if name not in ever:
                continue  # never had a picture since BigEye started (switched off): nothing to report
            since.setdefault(name, now)
            if now - since[name] > 600 and name not in told:
                told.add(name)
                push_alert(f"{name} camera offline", f"No picture from the {name} camera for 10 minutes. "
                           "Try Reconnect on the page; if that does not help, check the camera's power.", "action", name)
        time.sleep(10)


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
                item["ptz"] = {"x": p["x"], "y": p["y"], "known": p["known"],
                               "restarted": AUTOFIX_STATE.get(name, {}).get("restart_seen", 0),
                               "fixing": AUTOFIX_STATE.get(name, {}).get("running", False),
                               "refs": sum(os.path.exists(ref_path(name, x["name"])) for x in p["presets"])}
                if st.get("follow"):
                    item["follow"] = st["follow"]
            base = CONTROLS.get(name)
            if base:
                try:
                    with urllib.request.urlopen(f"{base}/status", timeout=2) as r:
                        st = json.load(r)
                    item["reachable"] = bool(st.get("connected", True))
                    if st.get("status_seen"):
                        item["battery"] = st.get("battery_pct")
                        item["charging"] = st.get("charging")
                except Exception:
                    item["reachable"] = False
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
            if cfg:  # the new bridge (bigeye-doorbridge) names two flags differently
                cfg.setdefault("no_sleep_on_power", cfg.get("no_sleep_on_charger"))
                cfg.setdefault("lan_wakeup", cfg.get("wake_from_lan"))
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
        if what in ("move", "goto", "go", "realign"):  # the owner is steering: follow must not pull back
            MANUAL_UNTIL[name] = time.time() + MANUAL_PAUSE
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
                save_reference(name, label)  # the picture it should show, for auto-fix after a restart
                return self._json(s)
            if what == "delete":
                s["presets"] = [p for p in s["presets"] if p["name"] != q.get("name")]
                save_settings()
                return self._json(s)
            if what == "reference":  # the camera shows the right view of this favourite now: remember it
                if not any(p["name"] == q.get("name") for p in s["presets"]):
                    return self._send(404, b"no such position")
                save_reference(name, q["name"])
                return self._json({"saved": q["name"]})
            if what == "autofix":
                MANUAL_UNTIL[name] = time.time() + 120
                return self._json(autofix_positions(name))
            if what == "lock":
                s["locked"] = q.get("on") == "1"
                save_settings()
                if s["locked"]:
                    lock_enforce(name)
                log(f"{name}: position lock {'on' if s['locked'] else 'off'}")
                return self._json(s)
            if what == "realign":
                # After a camera restart the motor is calibrated again and every saved position is off
                # by the same amount. The owner aims at where one favourite should look; shift them all.
                ref = next((p for p in s["presets"] if p["name"] == q.get("name")), None)
                if not ref:
                    return self._send(404, b"no such position")
                dx, dy = s["x"] - ref["x"], s["y"] - ref["y"]
                for p in s["presets"]:
                    p["x"] = round(min(1.0, max(0.0, p["x"] + dx)), 4)
                    p["y"] = round(min(1.0, max(0.0, p["y"] + dy)), 4)
                save_settings()
                log(f"{name}: favourites realigned from '{ref['name']}' by pan {dx:+.3f}, tilt {dy:+.3f}")
                return self._json(dict(s, shift={"x": round(dx, 4), "y": round(dy, 4)}))
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
                zones = [z for z in (clean_shape(z) for z in body["zones"][:20]) if z]
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
            if "at_home" in body:  # keep recording in Home mode (outdoor cameras: door, window)
                m["at_home"] = bool(body["at_home"])
            if body.get("record_on") in ("motion", "person", "person_animal", "animal"):
                m["record_on"] = body["record_on"]
            if body.get("look_for") in ("people", "all"):
                m["look_for"] = body["look_for"]
            if "sens" in body:
                m["sens"] = max(1, min(100, int(body["sens"])))
            if isinstance(body.get("auto"), dict):
                a = settings(name).setdefault("auto", {})
                for k in ("night", "quality", "timelapse"):
                    if k in body["auto"]:
                        a[k] = bool(body["auto"][k])
            if isinstance(body.get("privacy"), list):
                rects = [z for z in (clean_shape(z) for z in body["privacy"][:12]) if z]
                settings(name)["privacy"] = rects
                CAMS[name].privacy = rects
                CAMS[name].restart_reader()
            if "osd" in body:
                settings(name)["osd"] = bool(body["osd"])
                CAMS[name].osd = bool(body["osd"])
                CAMS[name].restart_reader()
            if isinstance(body.get("label"), str):   # the name shown on the page, in the clock and in alerts
                label = " ".join(body["label"].split())[:24]
                if label:
                    settings(name)["label"] = label
                else:
                    settings(name).pop("label", None)
                CAMS[name].restart_reader()       # the clock in the picture shows the name too
            if "rotate" in body or "mirror" in body:   # BigEye turns the picture itself: works for every camera
                o = settings(name).setdefault("orient", {"rotate": 0, "mirror": False})
                if body.get("rotate") in (0, 90, 180, 270):
                    o["rotate"] = body["rotate"]
                if "mirror" in body:
                    o["mirror"] = bool(body["mirror"])
                CAMS[name].orient = dict(o)
                CAMS[name].restart_reader()
            if any(k in body for k in ("deflicker", "denoise", "night_boost", "hide_clock")):
                enh = settings(name).setdefault("enhance", {})
                for k in ("deflicker", "denoise", "hide_clock"):
                    if k in body:
                        enh[k] = bool(body[k])
                if "night_boost" in body:
                    enh["night_boost"] = max(0, min(2, int(body["night_boost"])))
                CAMS[name].enhance = dict(enh)
                CAMS[name].restart_reader()
            if "brightness" in body or "contrast" in body:
                pic = settings(name).setdefault("picture", {"brightness": 0, "contrast": 0})
                for key in ("brightness", "contrast"):
                    if key in body:
                        pic[key] = max(-100, min(100, int(body[key])))
                CAMS[name].eq = (pic["brightness"], pic["contrast"])
                CAMS[name].restart_reader()
            save_settings()
        self._json(dict(m, record_on=m.get("record_on", "person"), look_for=m.get("look_for", "all"), ai=m.get("ai", True), skip_known=m.get("skip_known", True), at_home=m.get("at_home", False),
                        picture=settings(name).get("picture", {"brightness": 0, "contrast": 0}),
                        enhance=settings(name).get("enhance", {}), auto=settings(name).get("auto", {}),
                        privacy=settings(name).get("privacy", []), osd=settings(name).get("osd", False),
                        orient=settings(name).get("orient", {"rotate": 0, "mirror": False})))

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


    # ---- phone app
    remote = False  # True in AppHandler (the phone's listener)

    def _app(self, what):
        q = self._query()
        if what == "alerts":  # long-poll: ?after=<id>&wait=<s>; after=-1 -> only the newest id
            after = int(q.get("after", "-1") or -1)
            if after < 0:
                return self._json({"alerts": [], "last": ALERT_SEQ[0]})
            if after > ALERT_SEQ[0]:
                after = 0  # BigEye restarted: numbering began again
            deadline = time.time() + min(55.0, float(q.get("wait", "0") or 0))
            with ALERT_COND:
                while ALERT_SEQ[0] <= after and time.time() < deadline:
                    ALERT_COND.wait(max(0.1, deadline - time.time()))
                new = [a for a in ALERTS if a["id"] > after]
            return self._json({"alerts": new, "last": ALERT_SEQ[0]})
        if what == "pair-code" and not self.remote:  # shown on the page on this PC only
            PAIR_CODE.update(code="".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)),
                             until=time.time() + 600)
            return self._json({"code": PAIR_CODE["code"], "valid_s": 600, "apk": os.path.exists(APK_FILE),
                               "address": CFG.get("app", {}).get("phone_url", "")})
        if what == "pair":  # the app trades the code (shown on the PC) for its own long key
            body = self._body_json() or {}
            code = str(body.get("code") or q.get("code") or "").strip().upper()
            if not (PAIR_CODE["code"] and time.time() < PAIR_CODE["until"] and hmac.compare_digest(code, PAIR_CODE["code"])):
                time.sleep(1.5)
                return self._json({"error": "wrong or expired code; make a new one on the PC"}, 403)
            PAIR_CODE.update(code=None, until=0)
            key = secrets.token_urlsafe(32)
            name = str(body.get("name") or q.get("name") or "phone")[:40]
            with DEVICES_LOCK:
                devices = load_devices()
                devices.append({"id": secrets.token_hex(4), "name": name, "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
                                "paired": round(time.time()), "last_seen": round(time.time())})
                save_devices(devices)
            log(f"phone app: paired '{name}'")
            push_alert("Phone paired", f"'{name}' can now open BigEye and get alerts.", "info")
            return self._json({"key": key, "name": name})
        if what == "test-alert" and not self.remote:  # a real-looking alert, with a picture, marked as a test
            cam = q.get("cam") or next((n for n, c in CAMS.items() if c.frame), None)
            pic = None
            if cam and CAMS[cam].frame:
                stamp = datetime.datetime.now()
                path = os.path.join(save_dir("motion"), f"{cam}-test-{stamp.strftime('%Y%m%d-%H%M%S')}.jpg")
                with open(path, "wb") as handle:
                    handle.write(CAMS[cam].frame)
                pic = os.path.relpath(path, SAVE_ROOT)
            push_alert(f"TEST: Stranger at the {cam} camera", f"Stranger at {time.strftime('%H:%M:%S')} (you are Away). "
                       "This is only a test from BigEye.", "urgent", cam, pic)
            if q.get("offsite") == "1" and cam and offsite_dir():   # also test the off-site copy (about 15 pictures)
                threading.Thread(target=offsite_copy, args=(cam, CAMS[cam].frame, "TEST"), daemon=True).start()
            return self._json({"ok": True, "cam": cam, "picture": pic, "offsite": bool(q.get("offsite") == "1" and offsite_dir())})
        if what == "info" and not self.remote:
            return self._json({"address": CFG.get("app", {}).get("phone_url", ""), "apk": os.path.exists(APK_FILE)})
        if what == "devices" and not self.remote:
            if q.get("remove"):
                with DEVICES_LOCK:
                    devices = [d for d in load_devices() if d["id"] != q["remove"]]
                    save_devices(devices)
            return self._json([{k: d.get(k) for k in ("id", "name", "paired", "last_seen")} for d in load_devices()])
        if what == "login":  # the app's WebView: key in a POST body -> cookie, then the page
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 400)).decode(errors="replace")
            key = urllib.parse.parse_qs(body).get("key", [""])[0]
            if not device_for_key(key):
                return self._send(401, b"not paired")
            return self._send(303, b"", extra={"Location": "/", "Set-Cookie":
                              f"bigeye_key={key}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"})
        if what == "download":  # the app itself (no secrets inside)
            if not os.path.exists(APK_FILE):
                return self._send(404, b"the app has not been built yet")
            with open(APK_FILE, "rb") as handle:
                data = handle.read()
            return self._send(200, data, "application/vnd.android.package-archive",
                              {"Content-Disposition": 'attachment; filename="sentinel-bigeye.apk"'})
        return self._send(404, b"unknown")

    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        if path == "":
            config = [{"name": n, "controls": n in CONTROLS, "features": sorted(FEATURES.get(n, ())),
                       # picture help needs the ffmpeg reader (not the MJPEG pass-through)
                       "enhance": not CAMS[n].passthrough(), "clock": bool(clock_box(n)), "label": cam_label(n)}
                      for n in CAMS]
            with open(PAGE_FILE, encoding="utf-8") as handle:
                page = handle.read()
            body = page.replace("__CONFIG__", json.dumps(config)).replace("__SAVEROOT__", json.dumps(win_path(SAVE_ROOT)))
            self._send(200, body.encode(), "text/html; charset=utf-8")
            return
        if path == "state":
            return self._state()
        if path.startswith("app/"):
            return self._app(path[4:])
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
            narr = ADDON_MODULES.get("narrator")
            out = []
            for e in list(EVENTS)[-40:][::-1]:
                d = narr.DESCRIPTIONS.get(e.get("picture") or "", {}).get("desc") if narr else None
                out.append(dict(e, desc=d) if d else e)
            return self._json(out)
        if path == "search":
            narr = ADDON_MODULES.get("narrator")
            fn = ADDON_ACTIONS.get(("narrator-search", "*"))
            q = (self._query().get("q") or "").strip()[:200]
            if not fn or not q:
                return self._json([])
            return self._json(fn(q))
        if path == "story":
            date = self._query().get("date") or time.strftime("%Y-%m-%d")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
                return self._send(400, b"date=YYYY-MM-DD")
            return self._json(day_story(date))
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
                elif len(parts) >= 2 and parts[1] == "unknown":  # people seen but not named (the AI groups them)
                    gid = urllib.parse.quote(q.get("id", ""))
                    if len(parts) == 3 and parts[2] == "picture":
                        with urllib.request.urlopen(f"http://127.0.0.1:8771/faces/unknown/picture?id={gid}", timeout=10) as reply:
                            return self._send(200, reply.read(), "image/jpeg")
                    if len(parts) == 3 and parts[2] == "name" and self.command == "POST":
                        req = urllib.request.Request(f"http://127.0.0.1:8771/faces/unknown/name?id={gid}&name="
                                                     f"{urllib.parse.quote(q.get('name', ''))}", data=b"", method="POST")
                    elif len(parts) == 3 and parts[2] == "delete" and self.command == "POST":
                        req = f"http://127.0.0.1:8771/faces/unknown/delete?id={gid}"
                    elif len(parts) == 2:
                        req = "http://127.0.0.1:8771/faces/unknown"
                    else:
                        return self.send_error(404)
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
            if len(parts) == 3 and parts[1] == "follow-learn" and settings(parts[2]).get("ptz", {}).get("locked"):
                return self._json({"pan": "locked", "tilt": "unlock the position first"})
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
        if path == "usage":
            return self._json(ADDON_STATUS.get("_usage") or {"waiting": True})
        if path == "autohome":
            page = SETTINGS.setdefault("_page", {})
            if self.command == "POST":
                body = self._body_json() or {}
                if not body:
                    page["auto_home"] = not page.get("auto_home", False)   # the header button: on/off
                if isinstance(body.get("on"), bool):
                    page["auto_home"] = body["on"]
                if isinstance(body.get("away_after_min"), int):
                    page["away_after_min"] = max(1, min(240, body["away_after_min"]))
                if isinstance(body.get("exit_cams"), list):
                    page["exit_cams"] = [c for c in body["exit_cams"] if c in CAMS]
                save_settings()
            return self._json({"on": page.get("auto_home", False), "away_after_min": page.get("away_after_min", 45),
                               "exit_cams": page.get("exit_cams") or ["door"]})
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
            if parts[2] == "tracking" and "on=1" in self.path and settings(parts[1]).get("ptz", {}).get("locked"):
                return self._send(423, b"locked: unlock the position first")
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


class AppHandler(Handler):
    """The phone's listener: everything needs a paired key (header X-BigEye-Key or the app's cookie),
    except pairing itself and downloading the app."""
    remote = True

    def do_GET(self):
        path = self.path.split("?")[0].strip("/")
        if path in ("app/pair", "app/login", "app/download"):
            return super().do_GET()
        key = self.headers.get("X-BigEye-Key") or ""
        if not key:
            for part in (self.headers.get("Cookie") or "").split(";"):
                k, _, v = part.strip().partition("=")
                if k == "bigeye_key":
                    key = v
        dev = device_for_key(key)
        if not dev:
            time.sleep(0.5)
            return self._send(401, b"Sentinel-BigEye: this device is not paired. Open the BigEye app and pair it.")
        note_presence(dev.get("name") if isinstance(dev, dict) else str(dev), self.headers.get("X-BigEye-Wifi"))
        if SETTINGS.get("_page", {}).get("auto_home") and phones_home() and not SETTINGS.get("_page", {}).get("home"):
            AddonContext.set_home(True, "auto: your phone is on the home Wi-Fi")   # at once, before any camera sees you
        return super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--app-port", type=int, default=0, help="second listener for the phone app (paired devices only)")
    ap.add_argument("--app-bind", default="127.0.0.1")
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
        opts = set()
        while url.rsplit(",", 1)[-1] in ("flip", "process"):
            url, opt = url.rsplit(",", 1)
            opts.add(opt)
        CAMS[name] = Camera(name, url, args.fps, args.quality, "flip" in opts)
        CAMS[name].process = "process" in opts
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
        cam.enhance = dict(settings(name).get("enhance", {}))
        cam.orient = dict(settings(name).get("orient", {}))
        cam.privacy = list(settings(name).get("privacy", []))
        # Every camera shows date/time top right: BigEye's stamp, unless the camera burns in its own clock there (360).
        cam.osd = bool(settings(name).setdefault("osd", not clock_box(name)))
        # The readers started before the settings were read (2026-11-15: clock and picture help missing after
        # every restart until toggled). Restart each reader now so it picks them up.
        cam.restart_reader()
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
    threading.Thread(target=lock_loop, daemon=True).start()
    threading.Thread(target=watch_restarts, daemon=True).start()
    threading.Thread(target=story_at_night, daemon=True).start()
    threading.Thread(target=camera_watch, daemon=True).start()
    print(f"Sentinel-BigEye: http://localhost:{args.port}/")
    if args.app_port:
        app_server = ThreadingHTTPServer((args.app_bind, args.app_port), AppHandler)
        threading.Thread(target=app_server.serve_forever, daemon=True).start()
        print(f"Sentinel-BigEye phone app listener: {args.app_bind}:{args.app_port}")
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
