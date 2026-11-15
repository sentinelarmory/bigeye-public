"""Follow add-on for Sentinel-BigEye: the 360 camera turns to keep a person in view.

Uses what the AI add-on found (ctx.status[cam]["ai"]["found"]) and the camera's
absolute position command (ctx.ptz_goto). When the person drifts away from the
middle of the picture, the camera is nudged toward them; small drifts are left
alone. After a while with nobody in view it can go back to a chosen favourite.

"Learn" moves the camera a little left/right and up/down and measures how far the
picture shifts (phase correlation), so it knows how much one step moves the view
on this camera and mounting (upside down or not). Without learning it uses
estimates from the lens (about 90 degrees wide over a 0..1 pan range).

Settings per camera (settings["follow"]): mode off | person | person_animal,
home (favourite name or ""), return_after seconds, gain_pan / gain_tilt.
"""
import io
import threading
import time

import numpy as np
from PIL import Image

DEFAULT_GAIN = {"pan": -4.0, "tilt": -2.0}  # picture shift (share of width/height) per 1.0 of position
DEADBAND = 0.13        # person this close to the middle (share of the picture): do not move
DAMPING = 0.7          # move 70 % of the way, so it does not overshoot
MAX_STEP = {"pan": 0.08, "tilt": 0.10}
SETTLE = 1.6           # seconds to let the motor stop before looking again
AIM_Y = 0.35           # aim at the upper part of a person (the face), not the belly
LABELS = {"person": {"person"}, "person_animal": {"person", "cat", "dog"}}


def grey(frame, w=480, h=270):
    img = Image.open(io.BytesIO(frame))
    img.draft("L", (w, h))
    a = np.asarray(img.convert("L").resize((w, h)), np.float32)
    a[: h // 9, int(w * 0.6):] = a.mean()  # hide the camera's clock
    return a - a.mean()


def shift(a, b):
    """How far the picture content moved from a to b, as share of width/height, plus how sure (peak)."""
    f = np.fft.fft2(a) * np.conj(np.fft.fft2(b))
    r = np.fft.ifft2(f / (np.abs(f) + 1e-6)).real
    y, x = np.unravel_index(r.argmax(), r.shape)
    peak = float(r.max() / (r.std() + 1e-9))
    if y > r.shape[0] // 2:
        y -= r.shape[0]
    if x > r.shape[1] // 2:
        x -= r.shape[1]
    return -x / r.shape[1], -y / r.shape[0], peak


class Addon:
    name = "follow"

    def __init__(self, ctx):
        self.ctx = ctx
        self.busy = {}

    def start(self):
        for name in self.ctx.cams:
            if self.ctx.has_ptz(name):
                self.ctx.register("follow-learn", name, lambda n=name: self.learn(n))
                threading.Thread(target=self._loop, args=(name,), daemon=True).start()

    def cfg(self, name):
        return self.ctx.settings(name).setdefault("follow", {"mode": "off", "home": "", "return_after": 60})

    def _frame_after(self, name, wait):
        time.sleep(wait)
        cam = self.ctx.cams[name]
        if time.time() - cam.stamp > 1.5:  # the picture stalled: a frozen frame would read as "no shift"
            raise RuntimeError("the picture is not live right now")
        return grey(cam.frame)

    def learn(self, name):
        """Move a little each way, measure the picture shift, store the gains. Returns a report."""
        if self.busy.get(name):
            return {"error": "already learning"}
        self.busy[name] = True
        try:
            return self._learn(name)
        except RuntimeError as error:
            return {"pan": str(error), "tilt": "try again in a moment"}
        finally:
            self.busy[name] = False

    def _learn(self, name):
        if True:
            pos = self.ctx.ptz_state(name)
            x0, y0 = pos["x"], pos["y"]
            out, sure = {}, {}
            for axis, step in (("pan", 0.04), ("tilt", 0.05)):
                gains, peaks = [], []
                for sign in (1, -1):
                    self.ctx.ptz_goto(name, x0, y0, quiet=8)
                    a = self._frame_after(name, 4)
                    dx, dy = (step * sign, 0) if axis == "pan" else (0, step * sign)
                    # Stay inside the camera's 0..1 range: go the other way near an end.
                    if not (0 <= x0 + dx <= 1 and 0 <= y0 + dy <= 1):
                        continue
                    self.ctx.ptz_goto(name, x0 + dx, y0 + dy, quiet=8)
                    b = self._frame_after(name, 4)
                    sx, sy, peak = shift(a, b)
                    if peak > 200:  # identical pictures: the camera did not move (or the picture froze)
                        continue
                    moved = sx if axis == "pan" else sy
                    gains.append(moved / (step * sign))
                    peaks.append(peak)
                if gains:
                    out[axis], sure[axis] = float(np.median(gains)), float(min(peaks))
            self.ctx.ptz_goto(name, x0, y0, quiet=4)
            c = self.cfg(name)
            report = {}
            for axis in ("pan", "tilt"):
                g, p = out.get(axis), sure.get(axis, 0)
                # A flat wall or ceiling gives no clear answer: keep the estimate then.
                same_way = g is not None and (g > 0) == (DEFAULT_GAIN[axis] > 0)
                if g is not None and 0.5 < abs(g) < 15 and (p > 8 or (p > 4 and same_way)):
                    c[f"gain_{axis}"] = round(g, 3)
                    report[axis] = f"learned {g:+.2f} (sure {p:.0f})"
                else:
                    report[axis] = f"not clear enough here (sure {p:.0f}); keeps {c.get(f'gain_{axis}', DEFAULT_GAIN[axis]):+.2f}"
            c["learned"] = time.strftime("%Y-%m-%d %H:%M")
            self.ctx.save()
            self.ctx.log(f"{name}: follow learned {report}")
            return report

    def _loop(self, name):
        last_seen, went_home, last_box = time.time(), True, None
        while True:
            time.sleep(0.4)
            c = self.cfg(name)
            st = self.ctx.status.setdefault(name, {})
            if c.get("mode", "off") == "off" or self.busy.get(name):
                st["follow"] = None
                continue
            if getattr(self.ctx, "locked", lambda n: False)(name):  # position locked: only the owner moves it
                st["follow"] = "off (position locked)"
                continue
            if getattr(self.ctx, "manual", lambda n: False)(name):  # the owner steers by hand: keep still
                st["follow"] = "paused while you steer"
                last_seen = time.time()
                continue
            ai = st.get("ai") or {}
            if ai.get("down") or time.time() - ai.get("t", 0) > 2 or self.ctx.quiet(name):
                continue
            people = [d for d in ai.get("found", []) if d["label"] in LABELS.get(c["mode"], set())]
            now = time.time()
            if not people:
                st["follow"] = "waiting"
                home = c.get("home")
                if home and not went_home and now - last_seen > c.get("return_after", 60):
                    if self.ctx.ptz_preset(name, home):
                        self.ctx.log(f"{name}: follow: nobody for {int(now - last_seen)} s, back to '{home}'")
                    went_home = True
                continue
            # Keep following the same one: the find nearest to the last box, else the biggest.
            def centre(b):
                return (b[0] + b[2]) / 2, b[1] + (b[3] - b[1]) * AIM_Y
            if last_box:
                lc = centre(last_box)
                target = min(people, key=lambda d: (centre(d["box"])[0] - lc[0]) ** 2 + (centre(d["box"])[1] - lc[1]) ** 2)
            else:
                target = max(people, key=lambda d: (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]))
            last_box, last_seen, went_home = target["box"], now, False
            cx, cy = centre(target["box"])
            ox, oy = cx - 0.5, cy - 0.5
            st["follow"] = f"following {target['label']}"
            if abs(ox) < DEADBAND and abs(oy) < DEADBAND:
                continue
            gp, gt = c.get("gain_pan", DEFAULT_GAIN["pan"]), c.get("gain_tilt", DEFAULT_GAIN["tilt"])
            # Content must move by -offset; position change = -offset / gain.
            dxp = 0.0 if abs(ox) < DEADBAND else max(-MAX_STEP["pan"], min(MAX_STEP["pan"], -ox / gp * DAMPING))
            dyp = 0.0 if abs(oy) < DEADBAND else max(-MAX_STEP["tilt"], min(MAX_STEP["tilt"], -oy / gt * DAMPING))
            pos = self.ctx.ptz_state(name)
            try:
                self.ctx.ptz_goto(name, pos["x"] + dxp, pos["y"] + dyp, quiet=SETTLE)
            except Exception as error:
                self.ctx.log(f"{name}: follow move failed: {error}")
            time.sleep(SETTLE)
