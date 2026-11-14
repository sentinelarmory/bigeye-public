"""Tests for the parts that work without cameras. Run: python3 -m pytest tests/"""
import importlib.util
import io
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "viewer"))
sys.path.insert(0, str(ROOT / "viewer" / "addons"))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---- motion add-on ---------------------------------------------------------------
def test_zone_mask_covers_the_drawn_rectangle():
    motion = load("viewer/addons/motion.py", "motion")
    mask = motion.zone_mask([[0.5, 0.0, 1.0, 1.0]])
    assert mask[:, : motion.W // 2 - 1].sum() == 0          # left half watched
    assert mask[:, motion.W // 2 + 1:].all()                 # right half ignored


def test_zone_mask_ignores_bad_input():
    motion = load("viewer/addons/motion.py", "motion")
    assert motion.zone_mask([["x", 1, 2, 3], None, [0, 0]]).sum() == 0


def test_sensitivity_lowers_the_threshold():
    motion = load("viewer/addons/motion.py", "motion")
    assert motion.needed_share(100) < motion.needed_share(50) < motion.needed_share(1)


def test_motion_add_on_triggers_outside_zones_only():
    pytest.importorskip("PIL")
    from PIL import Image, ImageDraw
    motion = load("viewer/addons/motion.py", "motion")

    def jpeg(box=None):
        im = Image.new("L", (1280, 720), 90)
        if box:
            ImageDraw.Draw(im).rectangle(box, fill=230)
        b = io.BytesIO(); im.save(b, "JPEG"); return b.getvalue()

    class Cam:
        frame, stamp = None, 0.0

    fired = []

    class Ctx:
        cams = {"t": Cam()}
        status = {}
        cfg = {"motion": {"on": True, "sens": 50, "zones": [[0.5, 0, 1, 1]]}}
        def settings(self, n): return self.cfg
        def quiet(self, n): return False
        def trigger(self, n, info): fired.append(info)
        def log(self, m): pass

    ctx = Ctx(); motion.Addon(ctx).start()
    cam = ctx.cams["t"]

    def feed(frame, seconds):
        end = time.time() + seconds
        while time.time() < end:
            cam.frame, cam.stamp = frame, time.time(); time.sleep(0.1)

    feed(jpeg(), 1.2)
    feed(jpeg((900, 200, 1200, 600)), 1.2)   # inside the ignore zone
    assert not fired
    feed(jpeg(), 1.5)
    feed(jpeg((100, 200, 400, 600)), 1.2)    # outside it
    assert fired and fired[0]["source"] == "motion"


# ---- AI add-on helpers ------------------------------------------------------------
def test_iou_and_zone_checks():
    ai = load("viewer/addons/ai.py", "ai")
    assert ai.iou([0, 0, 1, 1], [0, 0, 1, 1]) == pytest.approx(1)
    assert ai.iou([0, 0, 0.1, 0.1], [0.5, 0.5, 1, 1]) == 0
    assert ai.in_zone([0.6, 0.6, 0.8, 0.8], [[0.5, 0.5, 1, 1]])
    assert not ai.in_zone([0.0, 0.0, 0.2, 0.2], [[0.5, 0.5, 1, 1]])


# ---- call-home monitor ------------------------------------------------------------
def test_classify_known_lines():
    tel = load("viewer/telemetry.py", "telemetry")
    assert tel.classify("UPLOAD PUT bucket.oss.x/a.jpg 100 bytes") == "upload"
    assert tel.classify("HTTP POST device-we01.tange365.com/auth/password") == "password"
    assert tel.classify("socket.io ping from 10.42.0.172 (#5)") == "keepalive"
    assert tel.classify("UDP 32100 type 0x12 len 48") == "p2p"
    assert tel.classify("UDP 10102 1TEG type 3 len 40") == "iwf"


def test_running_counters_add_the_difference():
    tel = load("viewer/telemetry.py", "telemetry")
    t = tel.Telemetry.__new__(tel.Telemetry)          # no SSH thread
    import collections, threading
    t.events, t.seq, t.lock, t.last_counter = collections.deque(), 0, threading.Lock(), {}
    tel.CAMERA_BY_IP.update({"10.42.0.136": "door"})
    for n in (100, 200, 300):
        t._line("iwf", f"2026-01-01 10:00:0{n // 100} UDP 10102 1TEG type 3 len 40 from 10.42.0.136:4000 (#{n})")
    assert sum(e["n"] for e in t.events) == 1 + 100 + 100
    assert {e["cam"] for e in t.events} == {"door"}


# ---- settings -----------------------------------------------------------------------
def test_settings_defaults_and_overrides(tmp_path, monkeypatch):
    f = tmp_path / "bigeye.toml"
    f.write_text('[paths]\nsave_root = "/tmp/x"\n[cameras.door]\nip = "10.0.0.2"\n')
    monkeypatch.setenv("BIGEYE_CONFIG", str(f))
    cfg = load("viewer/bigeye_config.py", "bigeye_config")
    assert cfg.CFG["paths"]["save_root"] == "/tmp/x"
    assert cfg.CFG["paths"]["log_file"].endswith("sentinel-bigeye.log")   # default kept
    assert cfg.camera("door", "ip") == "10.0.0.2"
    assert cfg.camera("nope", "ip", "-") == "-"
