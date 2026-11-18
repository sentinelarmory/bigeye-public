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
# Face engine: "sface" = OpenCV Zoo YuNet (MIT) + SFace (Apache-2.0), free for closed products (default);
# "insightface" = buffalo_l, better by a hair but its pretrained models are for NON-COMMERCIAL use only.
# Each engine has its own fingerprints (they are not compatible), so each has its own faces file.
FACE_ENGINE = os.environ.get("BIGEYE_FACES", "sface")
FACES_FILE = os.path.expanduser("~/.config/sentinel-bigeye/" + ("faces.json" if FACE_ENGINE == "insightface" else "faces-sface.json"))
FACE_MATCH = 0.40 if FACE_ENGINE == "insightface" else 0.363  # cosine for "same person" (SFace: OpenCV's own cut)
FACE_DIM = 512 if FACE_ENGINE == "insightface" else 128
FACE_MIN_DET = 0.55    # how sure the face finder must be that it is a face
MAX_SAMPLES = 150   # enrolled + trained fingerprints per name (training adds at most TRAIN_CAP)
# Strangers: faces that match nobody are grouped (same person = cosine >= UNKNOWN_SAME) so the owner can name a
# repeat visitor with one click. Kept on this machine only; a group not seen for UNKNOWN_KEEP_DAYS is dropped.
UNKNOWN_DIR = os.path.expanduser("~/.local/share/sentinel-bigeye/unknown-faces")
UNKNOWN_SAME = 0.42 if FACE_ENGINE == "sface" else 0.45
UNKNOWN_MIN_DET = 0.80     # only clear faces go into the gallery
UNKNOWN_MIN_PX = 40        # face at least this wide (pixels)
UNKNOWN_KEEP_DAYS = 30
UNKNOWN_MAX = 200          # groups at most (the oldest unnamed go first)

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


class SFaceApp:
    """YuNet (finds faces) + SFace (128-number fingerprint) from OpenCV Zoo, on the processor (a few ms)."""
    def __init__(self):
        import cv2
        m = os.path.expanduser("~/.local/share/sentinel-bigeye/models/")
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(m + "face_detection_yunet_2023mar.onnx", "", (320, 320), FACE_MIN_DET, 0.3, 50)
        self.rec = cv2.FaceRecognizerSF.create(m + "face_recognition_sface_2021dec.onnx", "")

    def get(self, crop):
        self.det.setInputSize((crop.shape[1], crop.shape[0]))
        _, faces = self.det.detect(crop)
        out = []
        for f in (faces if faces is not None else []):
            emb = self.rec.feature(self.rec.alignCrop(crop, f)).flatten().astype(np.float32)
            emb /= (np.linalg.norm(emb) or 1.0)
            x, y, fw, fh = (float(v) for v in f[:4])
            out.append(type("Face", (), {"det_score": float(f[14]), "bbox": [x, y, x + fw, y + fh], "normed_embedding": emb}))
        return out


def face_app():
    """The face engine (see FACE_ENGINE), loaded on first use."""
    global _FACE_APP
    if _FACE_APP is None and FACE_ENGINE == "sface":
        _FACE_APP = SFaceApp()
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
            "px": [int(cx0 + fb[0]), int(cy0 + fb[1]), int(cx0 + fb[2]), int(cy0 + fb[3])],
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


UNKNOWN = {}   # id -> {"embs": array, "count", "first", "last", "best"}
UNKNOWN_LOCK = threading.Lock()


def load_unknown():
    try:
        with open(os.path.join(UNKNOWN_DIR, "index.json")) as handle:
            for k, v in json.load(handle).items():
                UNKNOWN[k] = dict(v, embs=np.asarray(v["embs"], np.float32))
    except (OSError, ValueError):
        pass


def save_unknown():
    os.makedirs(UNKNOWN_DIR, mode=0o700, exist_ok=True)
    tmp = os.path.join(UNKNOWN_DIR, "index.json.tmp")
    with open(tmp, "w") as handle:
        json.dump({k: dict(v, embs=v["embs"].round(5).tolist()) for k, v in UNKNOWN.items()}, handle)
    os.replace(tmp, os.path.join(UNKNOWN_DIR, "index.json"))
    os.chmod(os.path.join(UNKNOWN_DIR, "index.json"), 0o600)


def remember_stranger(f, bgr):
    """Put a clear face that matched nobody into its group (or a new one); keep the best picture of each group."""
    fx0, fy0, fx1, fy1 = f["px"]
    if f["det"] < UNKNOWN_MIN_DET or fx1 - fx0 < UNKNOWN_MIN_PX:
        return
    now = time.time()
    with UNKNOWN_LOCK:
        best_id, best_sim = None, 0.0
        for gid, g in UNKNOWN.items():
            sim = float((g["embs"] @ f["emb"]).max())
            if sim > best_sim:
                best_id, best_sim = gid, sim
        if best_id and best_sim >= UNKNOWN_SAME:
            g = UNKNOWN[best_id]
            if now - g["last"] < 10:        # same visit: at most one sample per 10 s
                g["last"] = now
                return
            g["embs"] = np.vstack([g["embs"], f["emb"][None]])[-12:]
            g["count"] += 1
            g["last"] = now
            gid = best_id
        else:
            gid = time.strftime("%Y%m%d-%H%M%S") + f"-{int(now * 1000) % 1000:03d}"
            g = UNKNOWN[gid] = {"embs": f["emb"][None], "count": 1, "first": now, "last": now, "best": 0.0}
        quality = f["det"] * (fx1 - fx0)
        if quality > g["best"]:             # keep the clearest picture, a little wider than the face
            import cv2
            h, w = bgr.shape[:2]
            pad = int((fx1 - fx0) * 0.35)
            crop = bgr[max(0, fy0 - pad):min(h, fy1 + pad), max(0, fx0 - pad):min(w, fx1 + pad)]
            if crop.size:
                os.makedirs(UNKNOWN_DIR, mode=0o700, exist_ok=True)
                cv2.imwrite(os.path.join(UNKNOWN_DIR, gid + ".jpg"), crop)
                g["best"] = quality
        # housekeeping: forget old, rarely seen groups; cap the total
        for k in [k for k, v in UNKNOWN.items() if now - v["last"] > UNKNOWN_KEEP_DAYS * 86400]:
            drop_unknown(k)
        while len(UNKNOWN) > UNKNOWN_MAX:
            drop_unknown(min(UNKNOWN, key=lambda k: (UNKNOWN[k]["count"], UNKNOWN[k]["last"])))
        save_unknown()


def drop_unknown(gid):
    UNKNOWN.pop(gid, None)
    try:
        os.remove(os.path.join(UNKNOWN_DIR, gid + ".jpg"))
    except OSError:
        pass


def name_unknown(gid, name):
    """The owner names a group: its fingerprints join that name's known faces."""
    with UNKNOWN_LOCK:
        g = UNKNOWN.get(gid)
        if not g:
            return {"error": "no such group"}
        samples = KNOWN.get(name, np.zeros((0, FACE_DIM), np.float32))
        KNOWN[name] = np.vstack([samples, g["embs"]])[-MAX_SAMPLES:]
        save_faces()
        drop_unknown(gid)
        save_unknown()
    return {"name": name, "samples": int(len(KNOWN[name]))}


load_unknown()


# Face training: while a known face is recognised, but only just (a new angle, other light, from above in the
# hallway), that fingerprint is added to the same name, so every camera knows you better over the days.
# Only clear, fairly sure matches are taken, a few per camera per day, each kept with its picture so it can be undone.
TRAIN_FILE = os.path.expanduser("~/.config/sentinel-bigeye/faces-trained.json")
TRAIN_DIR = os.path.expanduser("~/.local/share/sentinel-bigeye/trained-faces")
TRAIN_LO, TRAIN_HI = (0.45, 0.62) if FACE_ENGINE == "sface" else (0.50, 0.68)   # sure it is you, yet not a copy
TRAIN_MIN_DET, TRAIN_MIN_PX = 0.80, 36
TRAIN_PER_CAM_DAY, TRAIN_GAP_S, TRAIN_CAP = 6, 900, 80
TRAIN_LOCK = threading.Lock()


def load_train():
    try:
        with open(TRAIN_FILE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {"added": []}


TRAIN = load_train()


def save_train():
    os.makedirs(os.path.dirname(TRAIN_FILE), exist_ok=True)
    tmp = TRAIN_FILE + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(TRAIN, handle)
    os.replace(tmp, TRAIN_FILE)
    os.chmod(TRAIN_FILE, 0o600)


def maybe_train(name, sim, f, bgr, cam):
    fx0, fy0, fx1, fy1 = f["px"]
    if not (TRAIN_LO <= sim < TRAIN_HI) or f["det"] < TRAIN_MIN_DET or fx1 - fx0 < TRAIN_MIN_PX:
        return
    now, day = time.time(), time.strftime("%Y-%m-%d")
    with TRAIN_LOCK:
        added = TRAIN.setdefault("added", [])
        mine = [a for a in added if a["name"] == name]
        if len(mine) >= TRAIN_CAP:
            return
        cam_today = [a for a in mine if a["cam"] == cam and a["day"] == day]
        if len(cam_today) >= TRAIN_PER_CAM_DAY or (cam_today and now - cam_today[-1]["t"] < TRAIN_GAP_S):
            return
        import cv2
        h, w = bgr.shape[:2]
        pad = int((fx1 - fx0) * 0.35)
        crop = bgr[max(0, fy0 - pad):min(h, fy1 + pad), max(0, fx0 - pad):min(w, fx1 + pad)]
        pid = time.strftime("%Y%m%d-%H%M%S") + "-" + cam
        os.makedirs(TRAIN_DIR, exist_ok=True)
        if crop.size:
            cv2.imwrite(os.path.join(TRAIN_DIR, pid + ".jpg"), crop)
        samples = KNOWN.get(name, np.zeros((0, FACE_DIM), np.float32))
        KNOWN[name] = np.vstack([samples, f["emb"][None]])[-MAX_SAMPLES:]
        save_faces()
        added.append({"id": pid, "name": name, "cam": cam, "day": day, "t": round(now), "sim": round(sim, 3),
                      "emb": [round(float(x), 5) for x in f["emb"]]})
        save_train()


def untrain(pid):
    with TRAIN_LOCK:
        added = TRAIN.setdefault("added", [])
        a = next((x for x in added if x["id"] == pid), None)
        if not a:
            return {"error": "not found"}
        samples = KNOWN.get(a["name"])
        if samples is not None and len(samples):
            emb = np.asarray(a["emb"], np.float32)
            keep = np.abs(samples - emb).max(axis=1) > 1e-3
            KNOWN[a["name"]] = samples[keep]
            save_faces()
        added.remove(a)
        save_train()
        try:
            os.remove(os.path.join(TRAIN_DIR, os.path.basename(pid) + ".jpg"))
        except OSError:
            pass
    return {"ok": True}


def training_info():
    with TRAIN_LOCK:
        added = TRAIN.get("added", [])
        day = time.strftime("%Y-%m-%d")
        return {"total": len(added), "today": sum(1 for a in added if a["day"] == day), "cap": TRAIN_CAP,
                "per_cam_day": TRAIN_PER_CAM_DAY,
                "recent": [{k: a[k] for k in ("id", "name", "cam", "t", "sim")} for a in added[-40:]][::-1]}


def detect_with_faces(jpeg, faces=False, cam="", train=False):
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
                if train and cam:
                    try:
                        maybe_train(name, sim, f, bgr, cam)
                    except Exception as error:
                        d["train_error"] = str(error)[:80]
            else:
                try:
                    remember_stranger(f, bgr)
                except Exception as error:
                    d["gallery_error"] = str(error)[:80]
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
    samples = KNOWN.get(name, np.zeros((0, FACE_DIM), np.float32))
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
                               "faces": {"engine": FACE_ENGINE, "known": len(KNOWN), "strangers": len(UNKNOWN)},
                               "chain": [x.info() for x in CHAIN], "switches": list(SWITCHES)})
        if self.path.startswith("/faces/unknown"):
            from urllib.parse import urlsplit, parse_qs
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/faces/unknown/picture":
                gid = os.path.basename(q.get("id", ""))
                try:
                    data = open(os.path.join(UNKNOWN_DIR, gid + ".jpg"), "rb").read()
                except OSError:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if u.path == "/faces/unknown/delete":
                with UNKNOWN_LOCK:
                    drop_unknown(q.get("id", ""))
                    save_unknown()
            with UNKNOWN_LOCK:
                def near(v):
                    best, sim = None, 0.0
                    for nm, smp in KNOWN.items():
                        x = float((v["embs"] @ smp.T).max()) if len(smp) else 0.0
                        if x > sim:
                            best, sim = nm, x
                    return best, round(sim, 2)
                groups = sorted(({"id": k, "count": v["count"], "first": round(v["first"]), "last": round(v["last"]),
                                  "near": near(v)[0], "near_sim": near(v)[1]}
                                 for k, v in UNKNOWN.items()), key=lambda g: (-g["count"], -g["last"]))
            return self._json({"engine": FACE_ENGINE, "groups": groups})
        if self.path.startswith("/faces/training"):
            from urllib.parse import urlsplit, parse_qs
            u = urlsplit(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/faces/training/picture":
                try:
                    data = open(os.path.join(TRAIN_DIR, os.path.basename(q.get("id", "")) + ".jpg"), "rb").read()
                except OSError:
                    return self.send_error(404)
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if u.path == "/faces/training/undo":
                return self._json(untrain(q.get("id", "")))
            return self._json(training_info())
        if self.path.startswith("/faces/delete"):
            from urllib.parse import urlsplit, parse_qs
            name = parse_qs(urlsplit(self.path).query).get("name", [""])[0]
            KNOWN.pop(name, None)
            save_faces()
        if self.path.startswith("/faces"):
            return self._json({k: int(len(v)) for k, v in KNOWN.items()})
        # (the engine in use is reported by /faces/unknown and /status)
        self.send_error(404)

    def do_POST(self):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        if url.path == "/faces/unknown/name":
            name = q.get("name", "").strip()[:30]
            return self._json(name_unknown(q.get("id", ""), name) if name else {"error": "name missing"})
        if url.path not in ("/detect", "/enroll"):
            return self.send_error(404)
        if not 0 < length < 20_000_000:
            return self._json({"error": "JPEG body expected"}, 400)
        try:
            body = self.rfile.read(length)
            if url.path == "/enroll":
                name = q.get("name", "").strip()[:30]
                return self._json(enroll(body, name) if name else {"error": "name missing"})
            return self._json(detect_with_faces(body, faces=q.get("faces") == "1", cam=q.get("cam", "")[:20],
                                                train=q.get("train") == "1"))
        except Exception as error:
            return self._json({"error": str(error)}, 500)


if __name__ == "__main__":
    print(f"Sentinel-BigEye AI on http://{ADDR[0]}:{ADDR[1]}  models " + ", ".join(f"{d.name} ({d.device})" for d in CHAIN), flush=True)
    ThreadingHTTPServer(ADDR, Handler).serve_forever()
