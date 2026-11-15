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
