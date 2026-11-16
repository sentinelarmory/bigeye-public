"""Settings for Sentinel-BigEye, read from one TOML file.

Where: $BIGEYE_CONFIG, else ~/.config/sentinel-bigeye/bigeye.toml. Every value has a
generic default, so the page starts without a file; a real setup fills in its own
cameras, folders and links. The file stays on the machine (it is never in the repo).

Example (see bigeye.example.toml for all keys):

    [paths]
    save_root = "~/Videos/Sentinel-BigEye"

    [notify]
    ntfy_url = "http://192.0.2.10:8099"
    topic_file = "~/.config/sentinel-bigeye/ntfy-topic"

    [cameras.door]
    ip = "10.42.0.136"            # its address on the camera Wi-Fi (for the call-home monitor)
    unit = "bigeye-bridge-door"   # its bridge service on the camera machine (for reconnect)
"""
import os
import tomllib

PATH = os.path.expanduser(os.environ.get("BIGEYE_CONFIG", "~/.config/sentinel-bigeye/bigeye.toml"))

DEFAULTS = {
    "paths": {"save_root": "~/Videos/Sentinel-BigEye", "log_file": "~/.cache/sentinel-bigeye.log",
              "settings_file": "~/.config/sentinel-bigeye/settings.json"},
    "notify": {"ntfy_url": "", "topic_file": "~/.config/sentinel-bigeye/ntfy-topic"},
    "ai": {"url": "http://127.0.0.1:8771"},
    "camera_machine": {
        # How to run a command on the machine that hosts the camera Wi-Fi and the bridges.
        # Empty = the bridges run on this machine (one-machine mode).
        "ssh": [],
        "ssh_fallback": [],
        "stand_in_media_url": "",   # the 360 stand-in cloud's caught uploads, e.g. http://127.0.0.1:18081
        "logs": ["~/bigeye-fakecloud.log", "~/.cache/bigeye-iwfcloud.log", "~/bigeye-360-media/reports.jsonl"],
    },
    "cameras": {},
    "ptz": {"seed_presets": []},
    "windows": {"explorer": "/mnt/c/Windows/explorer.exe"},
    # The phone app: the address the phone uses for this PC's app listener (--app-port), shown when pairing.
    "app": {"phone_url": ""},
}


def _merge(base, extra):
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load():
    try:
        with open(PATH, "rb") as handle:
            return _merge(DEFAULTS, tomllib.load(handle))
    except FileNotFoundError:
        return DEFAULTS


CFG = load()


def path(section, key):
    return os.path.expanduser(CFG[section][key])


def camera(name, key, default=None):
    return CFG["cameras"].get(name, {}).get(key, default)


# ── video encoding ──────────────────────────────────────────────────────────
# H.264 for recordings and timelapses without GPL parts: the graphics card's own video encoder (NVENC, almost no
# processor load, separate from the cores the AI uses) first, Cisco's OpenH264 (BSD) as the processor fallback.
# libx264 (GPL) is used only if neither works, so a stock ffmpeg still records.
_ENCODER = []


def h264_args(quality=23):
    """ffmpeg output arguments for H.264 at about this quality (lower = better, like CRF)."""
    import subprocess
    if not _ENCODER:
        for enc in ("h264_nvenc", "libopenh264", "libx264"):
            try:
                r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                    "testsrc2=s=640x360:d=0.2", "-c:v", enc, "-f", "null", "-"],
                                   capture_output=True, timeout=20)
            except Exception:
                continue
            if r.returncode == 0:
                _ENCODER.append(enc)
                break
        else:
            _ENCODER.append("libx264")
    enc = _ENCODER[0]
    if enc == "h264_nvenc":
        # NVENC's cq is not libx264's crf: +3 gives slightly better pictures than libx264 at that crf (SSIM 0.991
        # vs 0.989 on a 360 clip, 2026-11-16) for ~1.5x the size, at about a fifth of the processor time.
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(quality + 3), "-b:v", "0", "-pix_fmt", "yuv420p"]
    if enc == "libopenh264":
        return ["-c:v", "libopenh264", "-b:v", "4M", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality), "-pix_fmt", "yuv420p"]


def h264_encoder():
    h264_args()
    return _ENCODER[0]
