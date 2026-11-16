"""Automation add-on for Sentinel-BigEye. Each part is off until switched on in the page.

  smart night   (door/window, iWFCam): night vision on after sunset, off after sunrise (sun times from [location] in bigeye.toml; without it 19:00-07:00)
  auto quality  (cameras with a quality control): low when the picture stutters, Full HD again when it is steady
  auto home     (page-wide): Home when a known face is seen; Away after a while with nobody, if the last person
                was seen by the door camera
  timelapse     (per camera): one small picture every 30 s, made into a video of the day after midnight

Settings: settings[cam]["auto"] = {"night": bool, "quality": bool, "timelapse": bool};
          settings["_page"]["auto_home"] = bool, ["away_after_min"] = minutes (default 45).
"""
import datetime
import io
import math
import os
import shutil
import subprocess
import threading
import time
import urllib.request

try:  # where the home is, for sunrise and sunset: [location] lat/lon in bigeye.toml (never in the code)
    from bigeye_config import CFG as _CFG
    _loc = _CFG.get("location", {})
except Exception:
    _loc = {}
try:
    from bigeye_config import h264_args
except Exception:  # standalone: plain libx264
    def h264_args(quality=23):
        return ["-c:v", "libx264", "-crf", str(quality), "-pix_fmt", "yuv420p"]
_lat, _lon = os.environ.get("BIGEYE_LAT", _loc.get("lat")), os.environ.get("BIGEYE_LON", _loc.get("lon"))
LAT, LON = (float(_lat), float(_lon)) if _lat is not None and _lon is not None else (None, None)
TIMELAPSE_EVERY = 30


def sun_times(day, lat=LAT, lon=LON):
    """Sunrise and sunset (local unix times) for a date; NOAA-style approximation, minutes accurate.
    Returns (None, None) in polar day/night."""
    n = day.timetuple().tm_yday
    g = 2 * math.pi / 365 * (n - 1)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g) - 0.006758 * math.cos(2 * g)
            + 0.000907 * math.sin(2 * g) - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqt = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g) - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    cos_h = (math.cos(math.radians(90.833)) / (math.cos(math.radians(lat)) * math.cos(decl))) - math.tan(math.radians(lat)) * math.tan(decl)
    if not -1 <= cos_h <= 1:
        return None, None
    h = math.degrees(math.acos(cos_h))
    noon_utc_min = 720 - 4 * lon - eqt
    midnight_utc = datetime.datetime(day.year, day.month, day.day, tzinfo=datetime.timezone.utc).timestamp()
    return midnight_utc + (noon_utc_min - 4 * h) * 60, midnight_utc + (noon_utc_min + 4 * h) * 60


def is_dark(now=None):
    now = now or time.time()
    if LAT is None:  # no location set: night is 19:00 to 07:00
        hour = time.localtime(now).tm_hour
        return hour >= 19 or hour < 7
    rise, sset = sun_times(datetime.date.fromtimestamp(now))
    if rise is None:
        return datetime.date.fromtimestamp(now).month in (11, 12, 1)  # polar night vs midnight sun
    return now < rise - 900 or now > sset + 900  # a quarter of an hour of twilight on each side


class Addon:
    name = "auto"

    def __init__(self, ctx):
        self.ctx = ctx
        self.ir_sent = {}       # camera -> last night-vision state sent (True/False)
        self.quality = {}       # camera -> "high"/"low" as last sent
        self.counts = {}        # camera -> (frames, time) at the last check
        self.slow = {}
        self.fast = {}
        self.last_person = (0.0, None)   # (time, camera)
        self.last_known = 0.0

    def cfg(self, name):
        return self.ctx.settings(name).get("auto", {})

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._timelapse, daemon=True).start()

    # -------------------------------------------------------------------------------------
    def _loop(self):
        tick = 0
        while True:
            time.sleep(10)
            tick += 1
            for name, cam in self.ctx.cams.items():
                c = self.cfg(name)
                try:
                    if c.get("night") and self.ctx.has_control(name, "ir") and tick % 3 == 0:
                        want = is_dark()
                        if self.ir_sent.get(name) != want:
                            self.ctx.control(name, f"ir?on={1 if want else 0}")
                            self.ir_sent[name] = want
                            self.ctx.log(f"{name}: smart night -> night vision {'on' if want else 'off'}")
                    if c.get("quality"):
                        self._quality(name, cam)
                except Exception as error:
                    self.ctx.log(f"{name}: automation error: {error}")
            self._home()

    def _quality(self, name, cam):
        frames, then = self.counts.get(name, (cam.count, time.time()))
        now = time.time()
        self.counts[name] = (cam.count, now)
        if now - then < 5:
            return
        fps = (cam.count - frames) / (now - then)
        self.slow[name] = self.slow.get(name, 0) + 1 if fps < 3 else 0
        self.fast[name] = self.fast.get(name, 0) + 1 if fps > 8 else 0
        cur = self.quality.get(name, "high")
        if cur == "high" and self.slow[name] >= 6:        # a minute of stutter
            self.ctx.set_quality(name, "low")
            self.quality[name] = "low"
            self.ctx.log(f"{name}: auto quality -> low ({fps:.1f} fps)")
        elif cur == "low" and self.fast[name] >= 60:      # ten steady minutes
            self.ctx.set_quality(name, "high")
            self.quality[name] = "high"
            self.ctx.log(f"{name}: auto quality -> Full HD again")

    def _home(self):
        page = self.ctx.page_settings()
        now = time.time()
        for name in self.ctx.cams:
            for d in (self.ctx.status.get(name, {}).get("ai") or {}).get("found", []) or []:
                if d.get("label") == "person":
                    self.last_person = (now, name)
                    if d.get("name") and not d.get("pet"):
                        self.last_known = now
        if not page.get("auto_home"):
            return
        home = page.get("home", False)
        if not home and now - self.last_known < 30:
            self.ctx.set_home(True, "auto: a known face was seen")
        away_after = page.get("away_after_min", 45) * 60
        t, cam = self.last_person
        exits = page.get("exit_cams") or ["door"]   # cameras that see you leave (outside the door)
        if home and t and now - t > away_after and cam in exits:
            self.ctx.set_home(False, f"auto: nobody seen for {int((now - t) / 60)} min, last person was at the {cam} camera")

    # -------------------------------------------------------------------------------------
    def _timelapse(self):
        from PIL import Image
        built = ""
        while True:
            time.sleep(TIMELAPSE_EVERY)
            root = os.path.join(self.ctx.save_root(), "timelapse")
            today = time.strftime("%Y-%m-%d")
            for name, cam in self.ctx.cams.items():
                if not self.cfg(name).get("timelapse") or cam.frame is None or time.time() - cam.stamp > 5:
                    continue
                folder = os.path.join(root, "frames", today, name)
                os.makedirs(folder, exist_ok=True)
                try:
                    im = Image.open(io.BytesIO(cam.frame)).convert("RGB")
                    im.thumbnail((960, 540))
                    im.save(os.path.join(folder, time.strftime("%H%M%S") + ".jpg"), quality=80)
                except Exception:
                    pass
            if built != today:  # after midnight: yesterday's frames -> one video per camera
                built = today
                frames_root = os.path.join(root, "frames")
                for day in sorted(os.listdir(frames_root)) if os.path.isdir(frames_root) else []:
                    if day >= today:
                        continue
                    for name in os.listdir(os.path.join(frames_root, day)):
                        src = os.path.join(frames_root, day, name)
                        out = os.path.join(root, f"{name}-{day}.mp4")
                        r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-framerate", "30",
                                            "-pattern_type", "glob", "-i", os.path.join(src, "*.jpg"), *h264_args(26),
                                            "-movflags", "+faststart", out])
                        if r.returncode == 0:
                            shutil.rmtree(src, ignore_errors=True)
                            self.ctx.log(f"{name}: timelapse for {day} saved ({out})")
                    if not os.listdir(os.path.join(frames_root, day)):
                        os.rmdir(os.path.join(frames_root, day))
