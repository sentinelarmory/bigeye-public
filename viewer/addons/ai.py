"""AI add-on for Sentinel-BigEye: knows people from cats, dogs and a TV picture.

Sends each camera's newest picture, twice a second, to the local AI service
(bigeye-ai.py on 127.0.0.1:8771, YOLO11 on the graphics card) and keeps what it
found, so the page can draw boxes. Anything whose middle lies in an ignore zone
is dropped (draw a zone over the TV). A find that the camera's "Record when"
setting asks for calls ctx.trigger(name, {"source": "ai", "label": ...}).

Nothing leaves this PC.
"""
import json
import threading
import time
import urllib.request

SERVICE = "http://127.0.0.1:8771"
INTERVAL = 0.5
MIN_SCORE = {"person": 0.55, "cat": 0.45, "dog": 0.45}
WANTS = {"person": {"person"}, "person_animal": {"person", "cat", "dog"}, "animal": {"cat", "dog"}}


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


STICKY_SECONDS = 8  # a name stays on a person box this long after the face was last seen clearly


def in_poly(x, y, poly):
    inside = False
    for i in range(len(poly)):
        x1, y1 = poly[i]
        x2, y2 = poly[i - 1]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-9) + x1:
            inside = not inside
    return inside


def in_zone(box, zones):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for z in zones or []:
        if isinstance(z, dict):
            if z.get("poly") and in_poly(cx, cy, z["poly"]):
                return True
            continue
        x0, x1 = sorted((z[0], z[2]))
        y0, y1 = sorted((z[1], z[3]))
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            return True
    return False


class Addon:
    name = "ai"

    def __init__(self, ctx):
        self.ctx = ctx

    def start(self):
        for name in self.ctx.cams:
            threading.Thread(target=self._watch, args=(name,), daemon=True).start()

    def _watch(self, name):
        cam, last, down_since = self.ctx.cams[name], 0.0, None
        named = []  # [{"box", "name", "t"}]: people recognised lately, so the name follows them
        while True:
            slow = self.ctx.page_settings().get("ai_mode") == "cpu" if hasattr(self.ctx, "page_settings") else False
            time.sleep(INTERVAL * 4 if slow else INTERVAL)  # guard's slow mode: fewer pictures, no faces
            cfg = self.ctx.settings(name).get("motion", {})
            status = self.ctx.status.setdefault(name, {})
            if not cfg.get("ai", True) or cam.frame is None or cam.stamp == last:
                if not cfg.get("ai", True):
                    status["ai"] = None
                continue
            last = cam.stamp
            if self.ctx.quiet(name):  # turning: the picture is a blur
                continue
            try:
                req = urllib.request.Request(f"{SERVICE}/detect?faces={0 if slow else 1}", data=cam.frame, headers={"Content-Type": "image/jpeg"})
                with urllib.request.urlopen(req, timeout=3) as reply:
                    found = json.load(reply)
                if down_since:
                    self.ctx.log("AI service answering again")
                down_since = None
            except Exception as error:
                if not down_since:
                    down_since = time.time()
                    self.ctx.log(f"AI service not answering ({error}); motion-only until it is back")
                status["ai"] = {"down": True}
                time.sleep(5)
                continue
            zones = cfg.get("zones")
            # People (and cats) shown on a TV or laptop screen are not in the room: drop them.
            screens = [d["box"] for d in found if d.get("label") in ("tv", "laptop") and d.get("score", 0) >= 0.4]
            def on_screen(box):
                cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                return any(s[0] - 0.02 <= cx <= s[2] + 0.02 and s[1] - 0.02 <= cy <= s[3] + 0.02 for s in screens)
            status["screens"] = len(screens)
            kept = [d for d in found if d.get("label") not in ("tv", "laptop")
                    and d.get("score", 0) >= MIN_SCORE.get(d.get("label"), 0.6)
                    and not in_zone(d["box"], zones) and not on_screen(d["box"])]
            now = time.time()
            pets = self.ctx.pets() if hasattr(self.ctx, "pets") else {}
            for d in kept:  # the household's own animal gets its name (one cat in the house: every cat is it)
                if d["label"] in ("cat", "dog") and pets.get(d["label"]):
                    d["name"], d["pet"] = pets[d["label"]], True
            named = [n for n in named if now - n["t"] < STICKY_SECONDS]
            for d in kept:
                if d["label"] != "person":
                    continue
                if d.get("name"):
                    named = [n for n in named if n["name"] != d["name"]] + [{"box": d["box"], "name": d["name"], "t": now}]
                else:  # face not visible now: the person who was just recognised here keeps the name
                    best = max(named, key=lambda n: iou(n["box"], d["box"]), default=None)
                    if best and iou(best["box"], d["box"]) > 0.3:
                        d["name"], d["sticky"] = best["name"], True
                        best["box"] = d["box"]
            # One person, one box (2026-11-15): when walking, YOLO can give a second, shifted box on the same
            # person that is not "inside" the first enough for the AI service to drop it. An unnamed person box
            # that overlaps a named one is the same person: drop it, so the owner is not also "person 84 %".
            named_boxes = [d["box"] for d in kept if d["label"] == "person" and d.get("name")]
            def same_as_named(d):
                if d["label"] != "person" or d.get("name"):
                    return False
                b0 = d["box"]; area = max(1e-6, (b0[2] - b0[0]) * (b0[3] - b0[1]))
                for o in named_boxes:
                    inter = max(0, min(b0[2], o[2]) - max(b0[0], o[0])) * max(0, min(b0[3], o[3]) - max(b0[1], o[1]))
                    if iou(b0, o) > 0.2 or inter / area > 0.45:
                        return True
                return False
            dropped = [d for d in kept if same_as_named(d)]
            if dropped:
                kept = [d for d in kept if d not in dropped]
                status["doubles"] = status.get("doubles", 0) + len(dropped)
            status["ai"] = {"t": round(now, 1), "found": kept}
            wanted = WANTS.get(cfg.get("record_on", "person"), set())
            hits = [d for d in kept if d["label"] in wanted]
            if cfg.get("skip_known", True):  # people it knows by face are not recorded
                hits = [d for d in hits if not d.get("name")]
            if hits:
                best = hits[0]
                self.ctx.trigger(name, {"source": "ai", "label": best["label"], "score": best["score"], "box": best["box"],
                                        "name": best.get("name")})
