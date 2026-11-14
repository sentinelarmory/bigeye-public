"""Motion sensor add-on for Sentinel-BigEye.

Looks at the pictures the page already has (no extra stream from the camera),
four times a second, at 160x90 grey. A pixel counts as changed when it differs
from a slowly updated background picture. Ignore zones (rectangles drawn on the
page) are left out. When enough of the picture changes on two looks in a row,
the add-on calls ctx.trigger(cam_name, info) and the page starts recording.

An add-on is a module in this folder with a class Addon(ctx) that has a start()
method. ctx gives: ctx.cams (name -> Camera), ctx.settings(name) (that camera's
saved settings), ctx.quiet(name) (True while the camera itself is moving),
ctx.trigger(name, info), ctx.status (dict the page can show), ctx.log(msg).
"""
import io
import threading
import time

import numpy as np
from PIL import Image

W, H = 160, 90
PIXEL_THRESHOLD = 22      # grey levels a pixel must change to count
LIGHT_CHANGE = 0.55       # more than this share changed at once = lights or night vision switching
BACKGROUND_RATE = 0.08    # how fast the background follows the picture


def needed_share(sensitivity):
    """Sensitivity 1..100 -> share of the watched picture that must change (5 % .. 0.15 %)."""
    s = max(1, min(100, int(sensitivity))) / 100.0
    return 0.05 * (1 - s) + 0.0015


def zone_mask(zones):
    """True where motion is ignored. zones = [[x0, y0, x1, y1], ...] as 0..1 of the picture."""
    mask = np.zeros((H, W), dtype=bool)
    for z in zones or []:
        try:
            x0, y0, x1, y1 = (float(v) for v in z)
        except (TypeError, ValueError):
            continue
        x0, x1 = sorted((x0, x1))
        y0, y1 = sorted((y0, y1))
        mask[int(y0 * H):int(np.ceil(y1 * H)), int(x0 * W):int(np.ceil(x1 * W))] = True
    return mask


class Addon:
    name = "motion"

    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        for name in self.ctx.cams:
            threading.Thread(target=self._watch, args=(name,), daemon=True).start()

    def _watch(self, name):
        cam = self.ctx.cams[name]
        background, last_stamp, hits = None, 0.0, 0
        zones_key, mask = None, zone_mask([])
        while True:
            time.sleep(0.25)
            cfg = self.ctx.settings(name).get("motion", {})
            status = self.ctx.status.setdefault(name, {})
            if not cfg.get("on"):
                background, hits = None, 0
                status.update(level=None, active=False)
                continue
            frame, stamp = cam.frame, cam.stamp
            if frame is None or stamp == last_stamp:
                continue
            last_stamp = stamp
            if self.ctx.quiet(name):  # the camera is turning: everything changes, ignore it
                background, hits = None, 0
                continue
            try:
                img = Image.open(io.BytesIO(frame))
                img.draft("L", (W * 2, H * 2))  # let the JPEG decoder scale down (fast)
                grey = np.asarray(img.convert("L").resize((W, H), Image.BILINEAR), dtype=np.float32)
            except Exception:
                continue
            key = repr(cfg.get("zones"))
            if key != zones_key:
                zones_key, mask = key, zone_mask(cfg.get("zones"))
            if background is None or background.shape != grey.shape:
                background = grey
                continue
            changed = (np.abs(grey - background) > PIXEL_THRESHOLD) & ~mask
            watched = max(1, int((~mask).sum()))
            share = float(changed.sum()) / watched
            background += (grey - background) * BACKGROUND_RATE
            need = needed_share(cfg.get("sens", 50))
            status.update(level=round(min(1.0, share / need) * 100), active=share >= need)
            if share > LIGHT_CHANGE:  # lights, night vision or exposure: start over
                background, hits = grey, 0
                continue
            hits = hits + 1 if share >= need else 0
            if hits >= 2:
                ys, xs = np.nonzero(changed)
                box = [round(float(xs.min()) / W, 3), round(float(ys.min()) / H, 3),
                       round(float(xs.max() + 1) / W, 3), round(float(ys.max() + 1) / H, 3)]
                self.ctx.trigger(name, {"source": "motion", "share": round(share, 4), "box": box})
