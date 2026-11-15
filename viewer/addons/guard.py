"""Guard add-on for Sentinel-BigEye: measures what BigEye uses, prevents overload, and acts when something breaks.

Every 10 s it measures the whole machine (CPU, RAM, GPU memory/load) and BigEye's own share (the page and its
picture readers, the AI service, and the LM Studio models BigEye uses). The page shows it as a usage meter.

Prevention (always on):
  - vision_ok: the narrator only asks LM Studio for a picture description when there is room on the graphics card
    (free memory >= 7.5 GB, or the vision model is already loaded) and the brain's 8B embedder is not busy
  - the AI service runs under a CPU cap (systemd CPUQuota) so it can never take the whole machine

Safety net (acts by itself, max once per 15 minutes per action, every action logged and shown in the page):
  GPU lost       nvidia-smi fails, or the AI service fell back to the CPU while a GPU exists
                 -> unload LM Studio models, restart the AI service; still lost -> slow AI mode + alert
                    ("run wsl --shutdown in PowerShell")
  GPU memory full (> 92 %) -> unload the narrator's vision model, pause descriptions 5 min
  CPU overload   machine > 85 % for 1 min, or BigEye > 8 cores -> slow AI mode (+ alert)
  Back to normal after 5 quiet minutes.
"""
import collections
import json
import os
import subprocess
import threading
import time
import urllib.request

import psutil

VISION_KEYS = ("gemma-4-e4b", "gemma-4-12b")
BRAIN_EMBEDDER = "qwen3-embedding-8b"
GAP = 15 * 60


NVIDIA_SMI = next((p for p in ("/usr/lib/wsl/lib/nvidia-smi", "/usr/bin/nvidia-smi") if os.path.exists(p)), "nvidia-smi")  # services have a short PATH


def gpu():
    try:
        out = subprocess.run([NVIDIA_SMI, "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=8)
        used, total, util, temp = [float(v) for v in out.stdout.strip().splitlines()[0].split(",")]
        return {"ok": True, "used_mb": used, "total_mb": total, "util": util, "temp": temp}
    except Exception as error:
        return {"ok": False, "error": str(error)[:120]}


def lms_models():
    try:
        out = subprocess.run([os.path.expanduser("~/.lmstudio/bin/lms"), "ps", "--json"], capture_output=True, text=True, timeout=10)
        return [{"key": m.get("modelKey") or m.get("identifier"), "size_mb": round((m.get("sizeBytes") or 0) / 1e6),
                 "type": m.get("type"), "status": m.get("status")} for m in json.loads(out.stdout or "[]")]
    except Exception:
        return None


def lms_unload(key=None):
    cmd = [os.path.expanduser("~/.lmstudio/bin/lms"), "unload"] + ([key] if key else ["--all"])
    subprocess.run(cmd, capture_output=True, text=True, timeout=30)


class Addon:
    name = "guard"

    def __init__(self, ctx):
        self.ctx = ctx
        self.page_proc = psutil.Process(os.getpid())
        self.procs = {}           # pid -> psutil.Process (cpu_percent needs the same object between calls)
        self.last_action = {}
        self.actions = collections.deque(maxlen=30)
        self.over = collections.deque(maxlen=6)
        self.ai_fail = collections.deque(maxlen=3)   # 30 s of failing AI before acting
        self.calm_since = time.time()
        self.had_gpu = False
        psutil.cpu_percent(None)

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _proc(self, p):
        if p.pid not in self.procs:
            self.procs[p.pid] = p
            p.cpu_percent(None)
        return self.procs[p.pid]

    def _bigeye_processes(self):
        groups = {"page and picture readers": [], "AI service (people, animals, faces)": [], "LM Studio (AI models)": []}
        groups["page and picture readers"].append(self._proc(self.page_proc))
        for c in self.page_proc.children(recursive=True):
            groups["page and picture readers"].append(self._proc(c))
        for p in psutil.process_iter(["cmdline"]):
            cmd = " ".join(p.info.get("cmdline") or [])
            if "bigeye-ai.py" in cmd:
                groups["AI service (people, animals, faces)"].append(self._proc(p))
            elif ".lmstudio" in cmd:
                groups["LM Studio (AI models)"].append(self._proc(p))
        out = {}
        for name, plist in groups.items():
            cpu = ram = 0.0
            for p in plist:
                try:
                    cpu += p.cpu_percent(None)
                    ram += p.memory_info().rss
                except psutil.Error:
                    pass
            out[name] = {"cpu": round(cpu, 1), "ram_mb": round(ram / 1e6)}
        return out

    def _act(self, key, what):
        if time.time() - self.last_action.get(key, 0) < GAP:
            return False
        self.last_action[key] = time.time()
        self.actions.appendleft({"t": round(time.time()), "what": what})
        self.ctx.log("guard: " + what)
        return True

    def _alert(self, text):
        try:
            self.ctx.notify("BigEye guard", text)
        except Exception:
            pass

    def _loop(self):
        n = 0
        while True:
            time.sleep(10)
            n += 1
            try:
                self._tick(n)
            except Exception as error:
                self.ctx.log(f"guard error: {error}")

    def _tick(self, n):
        g = gpu()
        models = lms_models() if n % 3 == 1 or not hasattr(self, "_models") else self._models
        self._models = models
        try:
            with urllib.request.urlopen("http://127.0.0.1:8771/status", timeout=4) as r:
                ai = json.load(r)
        except Exception:
            ai = {"device": "down"}
        cpu = psutil.cpu_percent(None)
        vm = psutil.virtual_memory()
        mine = self._bigeye_processes()
        cores = psutil.cpu_count() or 1
        bigeye_cpu = sum(v["cpu"] for k, v in mine.items() if not k.startswith("LM Studio"))
        if g.get("ok"):
            self.had_gpu = True
        page = self.ctx.page_settings()
        mode = page.get("ai_mode", "normal")

        # ---- prevention: is there room for the narrator's vision model?
        loaded = {m["key"]: m for m in (models or [])}
        vision_loaded = any(any(v in (k or "") for v in VISION_KEYS) for k in loaded)
        brain_busy = any(BRAIN_EMBEDDER in (k or "") for k in loaded)
        free_mb = (g["total_mb"] - g["used_mb"]) if g.get("ok") else 0
        paused_until = getattr(self, "vision_pause", 0)
        vision_ok = g.get("ok") and time.time() > paused_until and not brain_busy and (vision_loaded or free_mb >= 7500)

        # ---- safety net
        problems = []
        # 2026-11-15: CUDA failure 999 inside the AI service: it still said "GPU" but every picture got HTTP 500.
        ai_failing = ai.get("device") == "GPU" and any((v or {}).get("ai", {}).get("down")
                                                       for k, v in list(self.ctx.status.items()) if not k.startswith("_"))
        self.ai_fail.append(ai_failing)
        gpu_lost = ((not g.get("ok") and self.had_gpu) or (ai.get("device") == "CPU" and g.get("ok"))
                    or (len(self.ai_fail) == self.ai_fail.maxlen and all(self.ai_fail)))
        if gpu_lost:
            problems.append("gpu")
            if self._act("gpu-restart", "graphics card lost for the AI: unloaded LM Studio models and restarted the AI service"):
                lms_unload()
                subprocess.run(["systemctl", "--user", "restart", "bigeye-ai"], timeout=60)
            elif self._act("gpu-alert", "graphics card still lost: AI slowed down; run 'wsl --shutdown' in PowerShell to get it back"):
                page["ai_mode"] = "cpu"
                self._alert("The graphics card is lost inside WSL. BigEye keeps running on the CPU (slower). "
                            "Fix: run 'wsl --shutdown' in PowerShell, then open Ubuntu again.")
        if g.get("ok") and g["used_mb"] / g["total_mb"] > 0.92:
            problems.append("vram")
            if vision_loaded and self._act("vram", f"graphics memory {g['used_mb']:.0f}/{g['total_mb']:.0f} MB: unloaded the description model, paused descriptions 5 min"):
                for k in loaded:
                    if any(v in k for v in VISION_KEYS):
                        lms_unload(k)
                self.vision_pause = time.time() + 300
                vision_ok = False
        self.over.append(cpu > 85 or bigeye_cpu > 800)
        if len(self.over) == self.over.maxlen and all(self.over):
            problems.append("cpu")
            if mode != "cpu" and self._act("cpu", f"machine busy ({cpu:.0f} % CPU, BigEye {bigeye_cpu:.0f} %): AI slowed down"):
                page["ai_mode"] = "cpu"
        if problems:
            self.calm_since = time.time()
        elif page.get("ai_mode") == "cpu" and time.time() - self.calm_since > 300:
            page["ai_mode"] = "normal"
            self.actions.appendleft({"t": round(time.time()), "what": "all quiet for 5 minutes: AI back to normal speed"})
            self.ctx.log("guard: back to normal")

        self.ctx.status["_usage"] = {
            "t": round(time.time()), "cores": cores,
            "machine": {"cpu": cpu, "ram_used_mb": round(vm.used / 1e6), "ram_total_mb": round(vm.total / 1e6),
                        "load": round(os.getloadavg()[0], 1)},
            "gpu": g, "bigeye": mine, "bigeye_cpu": round(bigeye_cpu, 1),
            "models": models, "ai_device": ai.get("device"), "ai_ms": ai.get("last_ms"),
            "ai_model": ai.get("kind") or ai.get("model"), "ai_licence": ai.get("licence"),
            "ai_chain": [c.get("kind") or c.get("name") for c in ai.get("chain", [])], "ai_switches": ai.get("switches", [])[:5],
            "ai_mode": page.get("ai_mode", "normal"), "vision_ok": bool(vision_ok),
            "problems": problems, "actions": list(self.actions)[:10],
        }
