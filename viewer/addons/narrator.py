"""Narrator add-on for Sentinel-BigEye: every event gets a short description, and becomes searchable.

Uses the local LM Studio server (OpenAI-compatible, http://127.0.0.1:1234):
  - a vision model (default gemma-4-e4b-it@q4_k_m, thinking off, ~0.5 s) looks at the event's picture
  - a small embedding model (default embeddinggemma) turns the description into numbers for search
Models load on first use and unload after 2 minutes without use (LM Studio "ttl").
Descriptions are kept next to the pictures in <save_root>/descriptions.jsonl. Nothing leaves this PC.
"""
import base64
import io
import json
import os
import threading
import time
import urllib.request

LMS = os.environ.get("BIGEYE_LMS_URL", "http://127.0.0.1:1234")
VISION = os.environ.get("BIGEYE_VISION_MODEL", "gemma-4-e4b-it@q4_k_m")
# Preferred first. EmbeddingGemma 2 needs an LM Studio llama.cpp runtime newer than 2.41; until it loads,
# the next one is used, and everything is embedded again with the better one as soon as it works.
EMBED_CANDIDATES = [m for m in os.environ.get("BIGEYE_EMBED_MODELS", "text-embedding-embeddinggemma-2@q8_0,"
                    "text-embedding-nomic-embed-text-v1.5").split(",") if m]
EMBED = None          # the one in use
_EMBED_CHECKED = 0.0
TTL = 120  # unload 2 minutes after the last use (the owner does not want models parked on the GPU)
SYSTEM = ("You describe home security camera pictures in one or two short, factual English sentences. "
          "Say who or what is there and what they are doing, and where in the room. Do not guess identities; "
          "use a name only if it is given to you.")

DESCRIPTIONS = {}   # picture path (relative to save_root) -> {"desc", "vec"}
LOCK = threading.Lock()


def _post(path, body, timeout=120):
    req = urllib.request.Request(LMS + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as reply:
        return json.load(reply)


def describe(jpeg, context):
    from PIL import Image
    im = Image.open(io.BytesIO(jpeg)); im.thumbnail((960, 540))
    buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=85)
    r = _post("/v1/chat/completions", {
        "model": VISION, "ttl": TTL, "temperature": 0.2, "max_tokens": 160, "reasoning_effort": "none",
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": [{"type": "text", "text": context + " Describe the picture."},
                                                  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}]}]})
    return (r["choices"][0]["message"].get("content") or "").strip()


def pick_embedder():
    """Use the best embedding model that loads; look again every 30 minutes (after a runtime update)."""
    global EMBED, _EMBED_CHECKED
    if EMBED == EMBED_CANDIDATES[0] or time.time() - _EMBED_CHECKED < 1800 and EMBED:
        return EMBED
    _EMBED_CHECKED = time.time()
    for m in EMBED_CANDIDATES:
        try:
            _post("/v1/embeddings", {"model": m, "ttl": TTL, "input": "test"}, timeout=120)
            EMBED = m
            return m
        except Exception:
            continue
    return EMBED


def embed(text):
    model = pick_embedder()
    if not model:
        raise RuntimeError("no embedding model loads")
    r = _post("/v1/embeddings", {"model": model, "ttl": TTL, "input": text}, timeout=60)
    return r["data"][0]["embedding"]


def write_text(prompt, max_tokens=700):
    r = _post("/v1/chat/completions", {
        "model": VISION, "ttl": TTL, "temperature": 0.4, "max_tokens": max_tokens, "reasoning_effort": "none",
        "messages": [{"role": "user", "content": prompt}]}, timeout=180)
    return (r["choices"][0]["message"].get("content") or "").strip()


class Addon:
    name = "narrator"

    def __init__(self, ctx):
        self.ctx = ctx
        self.file = os.path.join(ctx.save_root(), "descriptions.jsonl")
        try:
            with open(self.file, encoding="utf-8") as handle:
                for line in handle:
                    d = json.loads(line)
                    DESCRIPTIONS[d["picture"]] = {"desc": d["desc"], "vec": d.get("vec"), "model": d.get("model")}
        except (OSError, ValueError):
            pass
        ctx.register("narrator-search", "*", self.search)
        ctx.register("narrator-describe", "*", lambda pic: DESCRIPTIONS.get(pic, {}).get("desc"))

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        down_since = None
        while True:
            time.sleep(4)
            self._reembed()
            todo = [e for e in list(self.ctx.events())[-60:] if e.get("picture") and e["picture"] not in DESCRIPTIONS]
            if not (self.ctx.status.get("_usage") or {}).get("vision_ok", True):
                continue  # guard says: no room on the graphics card right now, or the brain is busy
            for e in todo[-20:]:
                path = os.path.join(self.ctx.save_root(), e["picture"])
                if not os.path.exists(path):
                    DESCRIPTIONS[e["picture"]] = {"desc": "", "vec": None}
                    continue
                who = e.get("label") or "movement"
                ctx = (f"Camera: {e['cam']}. Time: {time.strftime('%Y-%m-%d %H:%M', time.localtime(e['time']))}. "
                       f"What the detector saw: {who}.")
                try:
                    desc = describe(open(path, "rb").read(), ctx)
                    vec = embed(f"{e['cam']} {who}: {desc}") if desc else None
                    model = EMBED
                    if down_since:
                        self.ctx.log("narrator: LM Studio answering again")
                    down_since = None
                except Exception as error:
                    if not down_since:
                        down_since = time.time()
                        self.ctx.log(f"narrator: LM Studio not answering ({error}); events stay undescribed for now")
                    break
                with LOCK:
                    DESCRIPTIONS[e["picture"]] = {"desc": desc, "vec": vec, "model": model}
                    with open(self.file, "a", encoding="utf-8") as handle:
                        handle.write(json.dumps({"picture": e["picture"], "desc": desc, "vec": vec, "model": model}) + "\n")

    def _reembed(self):
        """After a better embedding model became available, embed the old descriptions again (a few per round)."""
        try:
            model = pick_embedder()
        except Exception:
            return
        stale = [k for k, d in list(DESCRIPTIONS.items()) if d.get("desc") and d.get("model") != model][:20]
        for k in stale:
            try:
                DESCRIPTIONS[k]["vec"] = embed(DESCRIPTIONS[k]["desc"])
                DESCRIPTIONS[k]["model"] = model
            except Exception:
                return
        if stale:
            with LOCK, open(self.file, "w", encoding="utf-8") as handle:
                for k, d in DESCRIPTIONS.items():
                    handle.write(json.dumps({"picture": k, "desc": d["desc"], "vec": d.get("vec"), "model": d.get("model")}) + "\n")

    def search(self, query):
        """Events ranked by meaning (embedding) with a word-match bonus."""
        import math
        try:
            q = embed(query)
        except Exception:
            q = None
        words = [w for w in query.lower().split() if len(w) > 2]
        hits = []
        for e in self.ctx.events():
            d = DESCRIPTIONS.get(e.get("picture") or "")
            if not d or not d.get("desc"):
                continue
            score = 0.0
            if q and d.get("vec") and d.get("model") == EMBED:  # never compare across embedding models
                v = d["vec"]
                dot = sum(a * b for a, b in zip(q, v))
                score = dot / (math.sqrt(sum(a * a for a in q)) * math.sqrt(sum(b * b for b in v)) + 1e-9)
            text = (d["desc"] + " " + (e.get("label") or "") + " " + e["cam"]).lower()
            score += 0.08 * sum(w in text for w in words)
            hits.append((score, dict(e, desc=d["desc"])))
        hits.sort(key=lambda h: -h[0])
        return [dict(h[1], score=round(h[0], 3)) for h in hits[:24]]
