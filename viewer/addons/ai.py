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


def in_zone(box, zones):
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for z in zones or []:
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
            time.sleep(INTERVAL)
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
                req = urllib.request.Request(f"{SERVICE}/detect?faces=1", data=cam.frame, headers={"Content-Type": "image/jpeg"})
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
            kept = [d for d in found if d.get("score", 0) >= MIN_SCORE.get(d.get("label"), 0.6)
                    and not in_zone(d["box"], zones)]
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
            status["ai"] = {"t": round(now, 1), "found": kept}
            wanted = WANTS.get(cfg.get("record_on", "person"), set())
            hits = [d for d in kept if d["label"] in wanted]
            if cfg.get("skip_known", True):  # people it knows by face are not recorded
                hits = [d for d in hits if not d.get("name")]
            if hits:
                best = hits[0]
                self.ctx.trigger(name, {"source": "ai", "label": best["label"], "score": best["score"], "box": best["box"],
                                        "name": best.get("name")})
