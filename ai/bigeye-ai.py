#!/usr/bin/env python3
"""Sentinel-BigEye AI: finds people and animals in a picture, on this PC only.

POST a JPEG to http://127.0.0.1:8771/detect -> JSON list of
{"label": "person", "score": 0.91, "box": [x0, y0, x1, y1]} with the box as 0..1 of the picture.
GET /status -> model, device (GPU or CPU) and speed.

Detectors: a list of COCO models as ONNX, tried in order (BIGEYE_MODELS, default
"rfdetr-small.onnx,yolo11s.onnx" in ~/.local/share/sentinel-bigeye/models). The first healthy one
is used; a model that fails, is slow (> 200 ms a picture over the last 20) or loses the graphics
card is swapped for the next one at once, and the first is tried again every 10 minutes.
RF-DETR (Apache-2.0) first means the detector BigEye normally uses is free for closed products too;
YOLO11 (AGPL-3.0) is only a fallback and can be left out of the list.
Run it with the venv in ~/.local/share/sentinel-bigeye/ai-venv (onnxruntime-gpu, numpy, pillow).
"""
import collections
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from PIL import Image

MODELS_DIR = os.path.expanduser("~/.local/share/sentinel-bigeye/models")
CHAIN_NAMES = [m.strip() for m in os.environ.get("BIGEYE_MODELS", os.environ.get("BIGEYE_MODEL", "rfdetr-small.onnx,yolo11s.onnx")).split(",") if m.strip()]
ADDR = ("127.0.0.1", int(os.environ.get("BIGEYE_AI_PORT", "8771")))
SLOW_MS = float(os.environ.get("BIGEYE_SLOW_MS", "200"))   # a GPU model slower than this (median of 20) is swapped
RETRY_FIRST_S = 600                                           # try the first model again this often
# The kinds of things we care about, by name (each model has its own class numbering).
WANTED_NAMES = ("person", "cat", "dog", "bird", "car", "tv", "laptop")  # tv/laptop: people on screens are not people
MIN_SCORE = 0.35
FACES_FILE = os.path.expanduser("~/.config/sentinel-bigeye/faces.json")  # name -> list of face fingerprints
FACE_MATCH = 0.40      # cosine similarity needed to call a face by name (ArcFace: 0.4 is a common cut)
FACE_MIN_DET = 0.55    # how sure the face finder must be that it is a face
MAX_SAMPLES = 60

try:  # CUDA libraries shipped as pip wheels (nvidia-*), if installed
    ort.preload_dlls()
except Exception:
    pass
providers = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in ort.get_available_providers()]
HAS_GPU = "CUDAExecutionProvider" in providers
LOCK = threading.Lock()
STATS = {"calls": 0, "last_ms": None}


class Detector:
    """One ONNX detector. Subclasses turn a picture into [(label, score, x0, y0, x1, y1 in pixels)]."""
    kind = "?"
    licence = "?"

    def __init__(self, path):
        self.path, self.name = path, os.path.basename(path)
        self.session, self.device, self.error = None, None, None
        self.times = collections.deque(maxlen=20)
        self.errors = 0

    def load(self, gpu=True):
        prov = providers if gpu else ["CPUExecutionProvider"]
        try:
            self.session = ort.InferenceSession(self.path, providers=prov)
        except Exception:
            self.session = ort.InferenceSession(self.path, providers=["CPUExecutionProvider"])
        self.device = "GPU" if self.session.get_providers()[0] == "CUDAExecutionProvider" else "CPU"
        self.input = self.session.get_inputs()[0].name
        self.times.clear()
        self.error = None
        return self

    def median_ms(self):
        return float(np.median(self.times)) if self.times else None

    def info(self):
        return {"name": self.name, "kind": self.kind, "licence": self.licence, "device": self.device,
                "median_ms": round(self.median_ms(), 1) if self.times else None, "errors": self.errors, "last_error": self.error}


class Yolo(Detector):
    kind, licence = "YOLO11", "AGPL-3.0"
    SIZE = 640
    CLASSES = {0: "person", 15: "cat", 16: "dog", 14: "bird", 2: "car", 62: "tv", 63: "laptop"}

    def raw(self, img, min_score):
        w, h = img.size
        scale = self.SIZE / max(w, h)
        nw, nh = int(round(w * scale)), int(round(h * scale))
        canvas = Image.new("RGB", (self.SIZE, self.SIZE), (114, 114, 114))
        px, py = (self.SIZE - nw) // 2, (self.SIZE - nh) // 2
        canvas.paste(img.resize((nw, nh), Image.BILINEAR), (px, py))
        arr = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
        out = self.session.run(None, {self.input: arr})[0][0].T  # (8400, 84): cx, cy, w, h, 80 class scores
        cls = out[:, 4:].argmax(1)
        score = out[np.arange(len(out)), 4 + cls]
        mask = (score >= min_score) & np.isin(cls, list(self.CLASSES))
        out, cls, score = out[mask], cls[mask], score[mask]
        cx, cy, bw, bh = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
        boxes = np.stack([(cx - bw / 2 - px) / scale, (cy - bh / 2 - py) / scale,
                          (cx + bw / 2 - px) / scale, (cy + bh / 2 - py) / scale], 1)
        return [self.CLASSES[int(c)] for c in cls], score, boxes, True   # True: needs NMS


class RFDetr(Detector):
    kind, licence = "RF-DETR", "Apache-2.0"
    # RF-DETR numbers its classes by COCO category id (with the gaps).
    CLASSES = {1: "person", 17: "cat", 18: "dog", 16: "bird", 3: "car", 72: "tv", 73: "laptop"}
    MEAN = np.array([0.485, 0.456, 0.406], np.float32)
    STD = np.array([0.229, 0.224, 0.225], np.float32)

    def load(self, gpu=True):
        super().load(gpu)
        self.size = self.session.get_inputs()[0].shape[2]
        return self

    def raw(self, img, min_score):
        w, h = img.size
        arr = ((np.asarray(img.resize((self.size, self.size), Image.BILINEAR), np.float32) / 255 - self.MEAN) / self.STD)
        dets, logits = self.session.run(None, {self.input: arr.transpose(2, 0, 1)[None]})
        prob = 1 / (1 + np.exp(-logits[0]))
        cls, score = prob.argmax(1), prob.max(1)
        mask = (score >= min_score) & np.isin(cls, list(self.CLASSES))
        d, cls, score = dets[0][mask], cls[mask], score[mask]
        boxes = np.stack([(d[:, 0] - d[:, 2] / 2) * w, (d[:, 1] - d[:, 3] / 2) * h,
                          (d[:, 0] + d[:, 2] / 2) * w, (d[:, 1] + d[:, 3] / 2) * h], 1)
        return [self.CLASSES[int(c)] for c in cls], score, boxes, False  # set-based: no NMS needed


def make_detector(name):
    path = name if os.path.isabs(name) else os.path.join(MODELS_DIR, name)
    return (RFDetr if "rfdetr" in os.path.basename(path).lower() else Yolo)(path)


CHAIN = []
for _n in CHAIN_NAMES:
    try:
        CHAIN.append(make_detector(_n).load())
    except Exception as _e:
        print(f"model {_n} could not be loaded: {_e}", flush=True)
if not CHAIN:
    raise SystemExit("no detector model could be loaded")
ACTIVE = [0]                          # index into CHAIN
SWITCHES = collections.deque(maxlen=20)
LAST_FIRST_TRY = [time.time()]


def active():
    return CHAIN[ACTIVE[0]]


def switch(to, why):
    if to == ACTIVE[0]:
        return
    SWITCHES.appendleft({"t": round(time.time()), "from": CHAIN[ACTIVE[0]].name, "to": CHAIN[to].name, "why": why})
    print(f"detector: {CHAIN[ACTIVE[0]].name} -> {CHAIN[to].name} ({why})", flush=True)
    ACTIVE[0] = to


def unhealthy(d):
    """Why a detector should not be used now, or None."""
    if d.session is None:
        return "not loaded"
    if HAS_GPU and d.device == "CPU":
        return "lost the graphics card"
    if d.device == "GPU" and len(d.times) >= 10 and d.median_ms() > SLOW_MS:
        return f"slow ({d.median_ms():.0f} ms a picture)"
    return None


def next_healthy(start):
    for i in list(range(start + 1, len(CHAIN))) + list(range(0, start)):
        if not unhealthy(CHAIN[i]):
            return i
    # nothing healthy on the graphics card: the fastest one on the processor keeps the cameras covered
    cpu = sorted(range(len(CHAIN)), key=lambda i: (CHAIN[i].kind != "YOLO11", i))
    return cpu[0]


def maybe_try_first(img):
    """Every 10 minutes: if a later model is in use, see whether the first one is healthy again."""
    if ACTIVE[0] == 0 or time.time() - LAST_FIRST_TRY[0] < RETRY_FIRST_S:
        return
    LAST_FIRST_TRY[0] = time.time()
    first = CHAIN[0]
    try:
        first.load()
        t0 = time.time()
        for _ in range(3):
            first.raw(img, MIN_SCORE)
        ms = (time.time() - t0) * 1000 / 3
        if not unhealthy(first) and ms <= SLOW_MS:
            switch(0, f"healthy again ({ms:.0f} ms)")
    except Exception as error:
        first.error = str(error)[:120]


_FACE_APP = None
FACE_LOCK = threading.Lock()


def face_app():
    """InsightFace buffalo_l (face finder + fingerprint), loaded on first use."""
    global _FACE_APP
    if _FACE_APP is None:
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "recognition"],
                           providers=[p for p in providers])
        app.prepare(ctx_id=0, det_size=(320, 320), det_thresh=FACE_MIN_DET)
        _FACE_APP = app
    return _FACE_APP


def load_faces():
    try:
        with open(FACES_FILE) as handle:
            return {k: np.asarray(v, np.float32) for k, v in json.load(handle).items()}
    except (OSError, ValueError):
        return {}


KNOWN = load_faces()


def save_faces():
    os.makedirs(os.path.dirname(FACES_FILE), exist_ok=True)
    tmp = FACES_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump({k: v.round(5).tolist() for k, v in KNOWN.items()}, handle)
    os.replace(tmp, FACES_FILE)
    os.chmod(FACES_FILE, 0o600)


def face_in(img_bgr, box, w, h):
    """The clearest face inside a person box (upper part), or None. box is 0..1."""
    x0, y0, x1, y1 = box
    bh = y1 - y0
    cx0, cy0 = max(0, int((x0 - 0.05) * w)), max(0, int((y0 - 0.05) * h))
    cx1, cy1 = min(w, int((x1 + 0.05) * w)), min(h, int((y0 + bh * 0.65) * h))
    if cx1 - cx0 < 24 or cy1 - cy0 < 24:
        return None
    crop = img_bgr[cy0:cy1, cx0:cx1]
    with FACE_LOCK:
        faces = face_app().get(crop)
    if not faces:
        return None
    f = max(faces, key=lambda f: float(f.det_score) * (f.bbox[2] - f.bbox[0]))
    emb = f.normed_embedding.astype(np.float32)
    fb = f.bbox
    return {"emb": emb, "det": float(f.det_score),
            "box": [round(float(cx0 + fb[0]) / w, 4), round(float(cy0 + fb[1]) / h, 4),
                    round(float(cx0 + fb[2]) / w, 4), round(float(cy0 + fb[3]) / h, 4)]}


def who(emb):
    best, sim = None, 0.0
    for name, samples in KNOWN.items():
        s = float((samples @ emb).max()) if len(samples) else 0.0
        if s > sim:
            best, sim = name, s
    return (best, sim) if sim >= FACE_MATCH else (None, sim)


def nms(boxes, scores, iou=0.5):
    order = scores.argsort()[::-1]
    keep = []
    while order.size:
        i = order[0]
        keep.append(i)
        xx0 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy0 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx1 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy1 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.clip(xx1 - xx0, 0, None) * np.clip(yy1 - yy0, 0, None)
        area = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
        overlap = inter / (area[i] + area[order[1:]] - inter + 1e-9)
        order = order[1:][overlap < iou]
    return keep


def run_chain(img, min_score):
    """Run the active detector; on an error or an unhealthy state, move to the next one and try again."""
    for _ in range(len(CHAIN) + 1):
        d = active()
        t0 = time.time()
        try:
            with LOCK:
                labels, score, boxes, needs_nms = d.raw(img, min_score)
        except Exception as error:
            d.errors += 1
            d.error = str(error)[:160]
            if len(CHAIN) == 1:
                raise
            switch(next_healthy(ACTIVE[0]), "error: " + d.error[:60])
            if "CUDA" in d.error or "cuda" in d.error:
                d.device = "CPU"  # its graphics-card context is broken until the service restarts
            continue
        ms = (time.time() - t0) * 1000
        d.times.append(ms)
        STATS["calls"] += 1
        STATS["last_ms"] = round(ms, 1)
        why = unhealthy(d)
        if why and len(CHAIN) > 1:
            nxt = next_healthy(ACTIVE[0])
            if nxt != ACTIVE[0]:
                switch(nxt, why)
        return labels, score, boxes, needs_nms
    raise RuntimeError("no detector works")


def detect(jpeg, min_score=MIN_SCORE):
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    maybe_try_first(img)
    labels, score, boxes, needs_nms = run_chain(img, min_score)
    if not len(labels):
        return [], img
    labels = np.array(labels)
    found = []
    for c in sorted(set(labels.tolist())):
        idx = np.where(labels == c)[0]
        order = nms(boxes[idx], score[idx]) if needs_nms else list(np.argsort(-score[idx]))
        kept = []
        for k in order:
            b = boxes[idx[k]]
            # A box lying mostly inside a bigger, surer box of the same kind is the same thing
            # (half body inside full body): drop it, so one person gets one box and one name.
            area = max(1e-9, (b[2] - b[0]) * (b[3] - b[1]))
            if any(max(0, min(b[2], o[2]) - max(b[0], o[0])) * max(0, min(b[3], o[3]) - max(b[1], o[1])) / area > 0.65 for o in kept):
                continue
            kept.append(b)
            found.append({"label": c, "score": round(float(score[idx[k]]), 3),
                          "box": [round(float(min(max(b[0] / w, 0), 1)), 4), round(float(min(max(b[1] / h, 0), 1)), 4),
                                  round(float(min(max(b[2] / w, 0), 1)), 4), round(float(min(max(b[3] / h, 0), 1)), 4)]})
    return sorted(found, key=lambda d: -d["score"]), img


def detect_with_faces(jpeg, faces=False):
    found, img = detect(jpeg)
    if not faces:
        return found
    people = [d for d in found if d["label"] == "person" and d["score"] >= 0.5]
    if not people:
        return found
    w, h = img.size
    bgr = np.asarray(img)[:, :, ::-1].copy()
    for d in people:
        try:
            f = face_in(bgr, d["box"], w, h)
        except Exception as error:
            d["face_error"] = str(error)[:80]
            continue
        if f:
            name, sim = who(f["emb"])
            d.update(face=f["box"], face_score=round(f["det"], 2), similarity=round(sim, 2))
            if name:
                d["name"] = name
    return found


def enroll(jpeg, name):
    """Add the face of the one person in the picture to name. Returns how many samples name has."""
    found, img = detect(jpeg)
    people = [d for d in found if d["label"] == "person" and d["score"] >= 0.5]
    if len(people) != 1:
        return {"error": f"need exactly one person in view, I see {len(people)}"}
    w, h = img.size
    f = face_in(np.asarray(img)[:, :, ::-1].copy(), people[0]["box"], w, h)
    if not f:
        return {"error": "no clear face: look toward the camera"}
    samples = KNOWN.get(name, np.zeros((0, 512), np.float32))
    KNOWN[name] = np.vstack([samples, f["emb"][None]])[-MAX_SAMPLES:]
    save_faces()
    return {"name": name, "samples": int(len(KNOWN[name])), "face_score": round(f["det"], 2)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/status"):
            d = active()
            return self._json({"model": d.name, "kind": d.kind, "licence": d.licence, "device": d.device, **STATS,
                               "chain": [x.info() for x in CHAIN], "switches": list(SWITCHES)})
        if self.path.startswith("/faces/delete"):
            from urllib.parse import urlsplit, parse_qs
            name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
            KNOWN.pop(name, None)
            save_faces()
        if self.path.startswith("/faces"):
            return self._json({k: int(len(v)) for k, v in KNOWN.items()})
        self.send_error(404)

    def do_POST(self):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if url.path not in ("/detect", "/enroll"):
            return self.send_error(404)
        if not 0 < length < 20_000_000:
            return self._json({"error": "JPEG body expected"}, 400)
        try:
            body = self.rfile.read(length)
            if url.path == "/enroll":
                name = q.get("name", "").strip()[:30]
                return self._json(enroll(body, name) if name else {"error": "name missing"})
            return self._json(detect_with_faces(body, faces=q.get("faces") == "1"))
        except Exception as error:
            return self._json({"error": str(error)}, 500)


if __name__ == "__main__":
    print(f"Sentinel-BigEye AI on http://{ADDR[0]}:{ADDR[1]}  models " + ", ".join(f"{d.name} ({d.device})" for d in CHAIN), flush=True)
    ThreadingHTTPServer(ADDR, Handler).serve_forever()
