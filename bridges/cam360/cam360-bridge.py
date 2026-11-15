#!/usr/bin/env python3
"""Local video bridge for the iCam365 / Tange 360 camera (CS2 PPCS, LAN only).

Talks to the camera the way the phone app does on the same network, without
any cloud: LanSearch -> punch echo -> P2P ready -> login -> start video.
Session layout and command ids follow the public notes in
github.com/jphein/eyeplus-icam365-camera (docs/driving-the-camera.md); the
video framing was confirmed from our own capture on 2026-11-13.

The password is the one the camera generates at boot and reports to its cloud;
our fake cloud (bigeye-fakecloud.py) stores it in ~/.config/sentinel-bigeye/cam360-pass.

Serves raw H.265 (Annex B) at http://127.0.0.1:8767/cam360.h265 starting at a
key frame, and a status page at /status. Log: ~/cam360-bridge.log
"""
import json
import os
import socket
import struct
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CAM_MAC = os.environ.get("CAM360_MAC", "").lower()  # the camera's Wi-Fi MAC (on its label or in your router)
CAMERA_IFACE = os.environ.get("CAMERA_IFACE", "wlan0")  # the camera Wi-Fi interface on this machine
PASS_FILE = os.path.expanduser("~/.config/sentinel-bigeye/cam360-pass")
LOG = os.path.expanduser("~/cam360-bridge.log")
HTTP_ADDR = ("127.0.0.1", 8767)
STREAM = int(os.environ.get("CAM360_STREAM", "2"))  # value sent with START (the app sent 2)

MSG_LAN_SEARCH, MSG_PUNCH_PKT, MSG_P2P_RDY = 0x30, 0x41, 0x42
MSG_DRW, MSG_DRW_ACK, MSG_ALIVE, MSG_ALIVE_ACK, MSG_CLOSE = 0xD0, 0xD1, 0xE0, 0xE1, 0xF0
IO_PASSWORD_REQ, IO_PASSWORD_RESP, IO_START, IO_GENERIC = 32770, 32771, 511, 1
CH_CMD, CH_VIDEO = 0, 2
CODEC_H265 = 80


def log(msg):
    with open(LOG, "a") as handle:
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def camera_ip():
    out = subprocess.run(["ip", "neigh", "show", "dev", CAMERA_IFACE], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if CAM_MAC in line.lower():
            return line.split()[0]
    return None


def pkt(kind, body=b""):
    return bytes([0xF1, kind]) + struct.pack(">H", len(body)) + body


class Hub:
    """Fans complete H.265 access units out to HTTP clients, each starting at a key frame."""

    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []
        self.frames = 0
        self.last_frame = 0.0
        self.state = "starting"

    def publish(self, data, key):
        self.frames += 1
        self.last_frame = time.time()
        with self.lock:
            for c in list(self.clients):
                if c["started"] or key:
                    c["started"] = True
                    try:
                        c["queue"].append(data)
                        c["event"].set()
                    except Exception:
                        pass


HUB = Hub()
COMMANDS = []  # (io_type, payload) queued by the HTTP API, sent on the live session
COMMANDS_LOCK = threading.Lock()
REPLIES = {}  # ioType -> newest reply from the camera (hex), for /replies

# Command ids and payloads decoded from the app session of 2026-11-13 (owner pressing every button).
PTZ_MOVE = 0x1001          # <u32 direction><u32 0>; 0 = stop
PTZ_DIRS = {"up": 1, "down": 2, "left": 3, "right": 6, "stop": 0}   # order seen: left, right, up, down
PRESET_GOTO = 0x408        # captured payloads per saved position (position floats + index)
PRESETS = {}  # positions are absolute: use /goto?x=&y= (the page saves favourites)
NIGHT_MODE_OLD = 0x310     # what we first took for night mode; no visible effect (kept for reference)
DAYNIGHT = 0x8018          # <u32 0><u32 mode><u32 0>: 0 = auto, 2 = night/IR (picture goes grey), 1 = day
CAMERA_ON = 0x414          # <u32 on><u32 0>: 0 stops all video (camera "off"), 1 = on. Was taken for tracking.
MOTION_TRACK = 0x8022      # <u32 0><u32 on>: auto-tracking (follows people, draws the green AI frame)
QUALITY = 0x320            # <u32 0><u32 1 = FHD | 5 = HD>


def queue(io_type, payload):
    with COMMANDS_LOCK:
        COMMANDS.append((io_type, payload))


class Session:
    def __init__(self, ip):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind(("0.0.0.0", 0))
        self.sock.settimeout(1.0)
        self.peer = None
        self.send_index = {}
        self.expect = {CH_VIDEO: 0}
        self.pending = {CH_VIDEO: {}}
        self.buffer = bytearray()
        self.cmd_replies = []
        self.seen = {}

    def send(self, data):
        self.sock.sendto(data, self.peer)

    def drw(self, channel, io_type, payload):
        idx = self.send_index.get(channel, 0)
        self.send_index[channel] = (idx + 1) & 0xFFFF
        body = struct.pack("<II", io_type, len(payload)) + payload
        self.send(pkt(MSG_DRW, bytes([0xD1, channel]) + struct.pack(">H", idx) + body))

    def connect(self):
        punch = None
        for _ in range(8):
            self.sock.sendto(pkt(MSG_LAN_SEARCH), (self.ip, 32108))
            try:
                data, addr = self.sock.recvfrom(2048)
            except socket.timeout:
                continue
            if addr[0] == self.ip and len(data) >= 24 and data[1] == MSG_PUNCH_PKT:
                punch, self.peer = data[4:24], addr
                break
        if punch is None:
            raise RuntimeError("camera did not answer LanSearch on UDP 32108")
        # Echo the camera's own identity back verbatim, then declare ready.
        self.send(pkt(MSG_PUNCH_PKT, punch))
        self.send(pkt(MSG_P2P_RDY, punch))
        log(f"session to {self.peer[0]}:{self.peer[1]}")

    def login(self, password):
        payload = bytearray(60)
        pw = password.encode()[:48]
        payload[8:8 + len(pw)] = pw
        self.drw(CH_CMD, IO_PASSWORD_REQ, bytes(payload))
        deadline = time.time() + 6
        while time.time() < deadline:
            self.pump(0.5)
            for io_type, body in self.cmd_replies:
                if io_type == IO_PASSWORD_RESP:
                    return True
                if io_type == IO_GENERIC and len(body) >= 8:
                    echoed, status = struct.unpack("<II", body[:8])
                    if echoed == IO_PASSWORD_REQ and status != 0:
                        raise RuntimeError(f"login refused (status {status})")
            self.cmd_replies.clear()
        raise RuntimeError("no answer to login")

    def ack(self, channel, index):
        self.send(pkt(MSG_DRW_ACK, bytes([0xD1, channel]) + struct.pack(">HH", 1, index)))

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, addr = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            if addr[0] != self.ip or len(data) < 4 or data[0] != 0xF1:
                continue
            kind = data[1]
            if kind == MSG_ALIVE:
                self.sock.sendto(pkt(MSG_ALIVE_ACK), addr)
            elif kind == MSG_CLOSE:
                raise RuntimeError("camera closed the session")
            elif kind == MSG_DRW and len(data) >= 8:
                channel = data[5]
                index = struct.unpack(">H", data[6:8])[0]
                self.ack(channel, index)
                body = data[8:]
                n = self.seen.get(channel, 0) + 1
                self.seen[channel] = n
                if n in (1, 50, 500):
                    log(f"DRW channel {channel}: packet #{n}, {len(body)} bytes, starts {body[:12].hex()}")
                if channel == CH_CMD and len(body) >= 8:
                    io_type, length = struct.unpack("<II", body[:8])
                    self.cmd_replies.append((io_type, body[8:8 + length]))
                    REPLIES[io_type] = {"t": round(time.time(), 1), "hex": body[8:8 + length].hex()}
                    log(f"command reply ioType {io_type} len {length}: {body[8:8 + min(length, 16)].hex()}")
                elif channel == CH_VIDEO:
                    self.video(index, body)

    def video(self, index, body):
        exp = self.expect[CH_VIDEO]
        if (index - exp) & 0xFFFF > 0x8000:
            return  # old duplicate
        self.pending[CH_VIDEO][index] = body
        while exp in self.pending[CH_VIDEO]:
            self.buffer += self.pending[CH_VIDEO].pop(exp)
            exp = (exp + 1) & 0xFFFF
        self.expect[CH_VIDEO] = exp
        if len(self.pending[CH_VIDEO]) > 400:  # a hole that never fills: resync at the next frame
            log("video gap, resyncing")
            self.pending[CH_VIDEO].clear()
            self.buffer.clear()
            self.expect[CH_VIDEO] = index
        self.frames_out()

    def frames_out(self):
        buf = self.buffer
        while len(buf) >= 16:
            codec, flags, _x, length, _ts = struct.unpack("<HHIII", buf[:16])
            if length > 4_000_000:  # lost alignment
                log("video framing lost, resyncing")
                buf.clear()
                return
            if len(buf) < 16 + length:
                return
            data = bytes(buf[16:16 + length])
            del buf[:16 + length]
            if codec == CODEC_H265 and data[:4] == b"\x00\x00\x00\x01":
                HUB.publish(data, key=bool(flags & 1))


def run():
    while True:
        ip = camera_ip()
        try:
            if not ip:
                raise RuntimeError("camera not on the camera Wi-Fi")
            password = open(PASS_FILE).read().strip()
            s = Session(ip)
            HUB.state = "connecting"
            s.connect()
            s.login(password)
            log("logged in")
            # What the app sent before video started: feature query (0x8024), two settings
            # reads (0x8030, 0x8032), then START with stream 2.
            s.drw(CH_CMD, 0x8024, b"")
            s.pump(0.3)
            s.drw(CH_CMD, IO_START, struct.pack("<II", STREAM, 0))
            s.pump(0.3)
            s.drw(CH_CMD, QUALITY, struct.pack("<II", 0, 1))  # ask for full HD
            # 0x414 off = camera sends no video at all (2026-11-14), so always switch it on.
            s.drw(CH_CMD, CAMERA_ON, struct.pack("<II", 1, 0))
            HUB.state = "streaming"
            started = time.time()
            last_alive = 0.0
            while True:
                s.pump(0.2)
                with COMMANDS_LOCK:
                    todo, COMMANDS[:] = list(COMMANDS), []
                for io_type, payload in todo:
                    s.drw(CH_CMD, io_type, payload)
                    log(f"sent command 0x{io_type:x} {payload.hex()}")
                now = time.time()
                if now - last_alive > 10:
                    s.send(pkt(MSG_ALIVE))
                    last_alive = now
                if now - max(HUB.last_frame, started) > 15:
                    raise RuntimeError("no video for 15 s")
        except Exception as error:
            HUB.state = f"retrying: {error}"
            log(f"{error}; retrying in 5 s")
            try:
                s.send(pkt(MSG_CLOSE))
            except Exception:
                pass
            time.sleep(5)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlsplit, parse_qs
        url = urlsplit(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path == "/ptz" and q.get("dir") in PTZ_DIRS:
            queue(PTZ_MOVE, struct.pack("<II", PTZ_DIRS[q["dir"]], 0))
            return self.reply(200, {"queued": "ptz " + q["dir"]})
        if url.path == "/preset" and q.get("n", "").isdigit() and int(q["n"]) in PRESETS:
            queue(PRESET_GOTO, bytes.fromhex(PRESETS[int(q["n"])]))
            return self.reply(200, {"queued": "preset " + q["n"]})
        if url.path == "/goto":  # absolute position: x = pan, y = tilt, both 0..1 (same floats as a preset)
            try:
                x, y, n = float(q["x"]), float(q["y"]), int(q.get("n", "1"))
            except (KeyError, ValueError):
                return self.reply(400, {"error": "use ?x=0..1&y=0..1"})
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 <= n <= 255):
                return self.reply(400, {"error": "x and y must be 0..1"})
            queue(PRESET_GOTO, struct.pack("<ffIII", x, y, 4, 0, n))
            return self.reply(200, {"queued": f"goto {x:.3f},{y:.3f}"})
        if url.path == "/raw" and q.get("t", "").isdigit():  # experiments only (bridge listens on localhost)
            try:
                payload = bytes.fromhex(q.get("p", ""))
            except ValueError:
                return self.reply(400, {"error": "p must be hex"})
            queue(int(q["t"]), payload)
            return self.reply(200, {"queued": f"raw {q['t']} {payload.hex()}"})
        if url.path == "/calibrate":  # the app's "pan-tilt calibration" (capture 2026-11-13): 0x1001 <u32 0x23><u32 0>
            queue(PTZ_MOVE, struct.pack("<II", 0x23, 0))
            return self.reply(200, {"queued": "pan-tilt calibration"})
        if url.path == "/pos":  # where the camera really points (0x40A -> reply 0x40B: <f32 x><f32 y>...)
            asked = time.time()
            queue(0x40A, b"")
            for _ in range(30):
                time.sleep(0.1)
                r = REPLIES.get(0x40B)
                if r and r["t"] >= round(asked, 1) - 0.1:
                    x, y = struct.unpack("<ff", bytes.fromhex(r["hex"])[:8])
                    return self.reply(200, {"x": round(x, 4), "y": round(y, 4)})
            return self.reply(504, {"error": "camera did not report its position"})
        if url.path == "/replies":
            return self.reply(200, {str(k): v for k, v in sorted(REPLIES.items())})
        if url.path == "/night" and q.get("mode") in ("on", "off", "auto"):
            mode = {"on": 2, "off": 1, "auto": 0}[q["mode"]]
            queue(DAYNIGHT, struct.pack("<III", 0, mode, 0))
            return self.reply(200, {"queued": "night " + q["mode"]})
        if url.path == "/tracking" and q.get("on") in ("0", "1"):
            queue(MOTION_TRACK, struct.pack("<II", 0, int(q["on"])))
            return self.reply(200, {"queued": "tracking " + q["on"]})
        if url.path == "/quality" and q.get("q") in ("fhd", "hd"):
            queue(QUALITY, struct.pack("<II", 0, 1 if q["q"] == "fhd" else 5))
            return self.reply(200, {"queued": "quality " + q["q"]})
        if self.path.startswith("/status"):
            body = json.dumps({"state": HUB.state, "frames": HUB.frames,
                               "last_frame_age": round(time.time() - HUB.last_frame, 1) if HUB.last_frame else None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/cam360.h265"):
            self.send_error(404)
            return
        client = {"queue": [], "event": threading.Event(), "started": False}
        with HUB.lock:
            HUB.clients.append(client)
        self.send_response(200)
        self.send_header("Content-Type", "video/H265")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                client["event"].wait(10)
                client["event"].clear()
                while client["queue"]:
                    self.wfile.write(client["queue"].pop(0))
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with HUB.lock:
                HUB.clients.remove(client)


if __name__ == "__main__":
    threading.Thread(target=run, daemon=True).start()
    log("bridge started")
    ThreadingHTTPServer(HTTP_ADDR, Handler).serve_forever()
