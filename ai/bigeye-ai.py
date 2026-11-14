#!/usr/bin/env python3
"""Sentinel-BigEye AI: finds people and animals in a picture, on this PC only.

POST a JPEG to http://127.0.0.1:8771/detect -> JSON list of
{"label": "person", "score": 0.91, "box": [x0, y0, x1, y1]} with the box as 0..1 of the picture.
GET /status -> model, device (GPU or CPU) and speed.

Model: YOLO11s (COCO classes) as ONNX, run with onnxruntime on the graphics card
when it can, otherwise on the processor. Run it with the venv in
~/.local/share/sentinel-bigeye/ai-venv (onnxruntime-gpu, numpy, pillow).
"""
import io
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import onnxruntime as ort
from PIL import Image

MODEL = os.path.expanduser(os.environ.get("BIGEYE_MODEL", "~/.local/share/sentinel-bigeye/models/yolo11s.onnx"))
ADDR = ("127.0.0.1", int(os.environ.get("BIGEYE_AI_PORT", "8771")))
SIZE = 640
# COCO class ids we care about (others are ignored).
WANTED = {0: "person", 15: "cat", 16: "dog", 14: "bird", 2: "car"}
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
try:
    SESSION = ort.InferenceSession(MODEL, providers=providers)
except Exception:
    SESSION = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
DEVICE = "GPU" if SESSION.get_providers()[0] == "CUDAExecutionProvider" else "CPU"
INPUT = SESSION.get_inputs()[0].name
LOCK = threading.Lock()
STATS = {"calls": 0, "last_ms": None}


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


def letterbox(img):
    w, h = img.size
    scale = SIZE / max(w, h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    canvas = Image.new("RGB", (SIZE, SIZE), (114, 114, 114))
    px, py = (SIZE - nw) // 2, (SIZE - nh) // 2
    canvas.paste(img.resize((nw, nh), Image.BILINEAR), (px, py))
    arr = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)[None] / 255.0
    return arr, scale, px, py


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


def detect(jpeg, min_score=MIN_SCORE):
    img = Image.open(io.BytesIO(jpeg)).convert("RGB")
    w, h = img.size
    arr, scale, px, py = letterbox(img)
    t0 = time.time()
    with LOCK:
        out = SESSION.run(None, {INPUT: arr})[0][0]  # (84, 8400): cx, cy, w, h, 80 class scores
    STATS["calls"] += 1
    STATS["last_ms"] = round((time.time() - t0) * 1000, 1)
    out = out.T
    cls = out[:, 4:].argmax(1)
    score = out[np.arange(len(out)), 4 + cls]
    mask = (score >= min_score) & np.isin(cls, list(WANTED))
    if not mask.any():
        return [], img
    out, cls, score = out[mask], cls[mask], score[mask]
    cx, cy, bw, bh = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
    boxes = np.stack([(cx - bw / 2 - px) / scale, (cy - bh / 2 - py) / scale,
                      (cx + bw / 2 - px) / scale, (cy + bh / 2 - py) / scale], 1)
    found = []
    for c in set(cls.tolist()):
        idx = np.where(cls == c)[0]
        for k in nms(boxes[idx], score[idx]):
            b = boxes[idx[k]]
            found.append({"label": WANTED[c], "score": round(float(score[idx[k]]), 3),
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
            return self._json({"model": os.path.basename(MODEL), "device": DEVICE, **STATS})
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
    print(f"Sentinel-BigEye AI on http://{ADDR[0]}:{ADDR[1]}  model {os.path.basename(MODEL)}  device {DEVICE}", flush=True)
    ThreadingHTTPServer(ADDR, Handler).serve_forever()
