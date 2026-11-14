#!/usr/bin/env python3
"""Warm fake cloud for the two iWFCam battery cameras (door and window) on the camera machine.

The cameras never reach the internet. Without answers they ask their cloud every
5 s forever ("0TEG" lookups to 47.105.85.47:10101), next to the LAN service the
bridges use. The firewall sends their cloud ports (UDP 10101-10103, any address)
here, and this answers like the real cloud does:

  UDP 10101  0TEG lookup -> 0TEG type 1 server list, learned once from the real cloud
             in relay mode and kept in ~/.config/sentinel-bigeye/iwfcloud-learned.json.
  UDP 10102  1TEG type 0 (register) and type 3 (status, every 30 s) -> type 1
             (account id, UID, heartbeat 30 s / timeout 150 s, server name);
             1TEG type 17 (heartbeat) -> type 17 with the time in UTC.
             All learned from the real cloud through relay mode on 2026-11-13.
  Anything else is logged and sampled.

Relay mode (to learn the real answers): ~/.config/sentinel-bigeye/iwfcloud-relay holds
"<camera UID> <until unix time>". Until then that camera's cloud packets (only
UDP 10101-10103) are passed to the real cloud through this machine and both
directions are logged to ~/bigeye-iwfcloud-relay.jsonl. The camera itself still
has no internet. The file is ignored once the time is past.

Log: ~/.cache/bigeye-iwfcloud.log   Samples: ~/bigeye-iwfcloud-samples.jsonl
"""
import asyncio, json, os, struct, time

HOST_IP = os.environ.get("BIGEYE_HOST_IP", "10.42.0.1")  # this machine on the camera Wi-Fi
LOG = os.path.expanduser("~/.cache/bigeye-iwfcloud.log")
os.makedirs(os.path.dirname(LOG), exist_ok=True)
SAMPLES = os.path.expanduser("~/bigeye-iwfcloud-samples.jsonl")
SAMPLES_PER_KIND = 5
RELAY_FILE = os.path.expanduser("~/.config/sentinel-bigeye/iwfcloud-relay")
RELAY_LOG = os.path.expanduser("~/bigeye-iwfcloud-relay.jsonl")
# Where the real cloud lives (from its own server list): lookups and servers.
REAL = {10101: ("47.105.85.47", 10101), 10102: ("47.254.26.168", 10102), 10103: ("47.254.26.168", 10103)}
# Camera addresses on the camera Wi-Fi (fixed DHCP leases), so heartbeats without
# a UID can be matched to their camera.
PORTS = {}  # local port -> Cloud, so upstream answers go back out the right port

# Answers learned from the real cloud through relay mode (see above), kept on this machine only:
# {"lookup": {uid: hex}, "status": hex, "camera_ips": {uid: ip}}. Nothing is shipped with the code.
LEARNED_FILE = os.path.expanduser("~/.config/sentinel-bigeye/iwfcloud-learned.json")


def _load_learned():
    try:
        with open(LEARNED_FILE) as handle:
            d = json.load(handle)
    except (OSError, ValueError):
        d = {}
    return ({u: bytes.fromhex(h) for u, h in d.get("lookup", {}).items()},
            bytes.fromhex(d["status"]) if d.get("status") else None, d.get("camera_ips", {}))


TEMPLATES, TYPE1, CAMERA_IPS = _load_learned()
_token = int.from_bytes(TYPE1[85:89], "little") if TYPE1 else 0


def save_learned():
    os.makedirs(os.path.dirname(LEARNED_FILE), exist_ok=True)
    with open(LEARNED_FILE, "w") as handle:
        json.dump({"lookup": {u: b.hex() for u, b in TEMPLATES.items()}, "status": TYPE1.hex() if TYPE1 else "",
                   "camera_ips": CAMERA_IPS}, handle)
    os.chmod(LEARNED_FILE, 0o600)





def type1_for(uid, cam_addr):
    """Register/status answer for a camera, like the real cloud's (None until learned)."""
    global _token
    if TYPE1 is None:
        return None
    b = bytearray(TYPE1)
    b[16:48] = uid.encode().ljust(32, b"\0")[:32]
    # Where the cloud saw the camera: here that is just its own address.
    b[52:56] = bytes(int(x) for x in cam_addr[0].split("."))
    b[56:58] = cam_addr[1].to_bytes(2, "big")
    _token += 57
    b[85:89] = (_token & 0xFFFFFFFF).to_bytes(4, "little")
    return bytes(b)


def time_reply():
    """Heartbeat answer: YY, month (0-11), day, weekday, hh, mm, ss in UTC."""
    t = time.gmtime()
    wday = (t.tm_wday + 1) % 7  # Sunday = 0
    return b"1TEG" + bytes([17, 0, 8, 0, t.tm_year % 100, t.tm_mon - 1, t.tm_mday, wday,
                            t.tm_hour, t.tm_min, t.tm_sec, 0])


UID_BY_IP = {}  # learned from lookups/registers, so status packets map to a UID


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    with open(LOG, "a") as f:
        f.write(line + "\n")


def masked(data):
    """Hex with the camera's password token (0x09 + 12 chars) blanked."""
    b = bytearray(data)
    i = b.find(0x09)
    while i != -1 and i + 13 <= len(b):
        if all(48 <= c < 128 for c in b[i + 1:i + 13]):
            b[i + 1:i + 13] = b"*" * 12
        i = b.find(0x09, i + 1)
    return bytes(b).hex()


def relay_uid():
    try:
        uid, until = open(RELAY_FILE).read().split()
        return uid if time.time() < float(until) else None
    except (OSError, ValueError):
        return None


class Upstream(asyncio.DatagramProtocol):
    """One socket per camera towards the real cloud; answers go back to the camera."""
    def __init__(self, cam_addr, uid):
        self.cam_addr, self.uid = cam_addr, uid

    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        local = next((p for p, (h, port) in REAL.items() if (h, port) == (addr[0], addr[1])), addr[1])
        cloud = PORTS.get(local) or PORTS.get(10102)
        cloud.t.sendto(data, self.cam_addr)
        magic, typ = kind_of(data)
        global TYPE1, _token
        if magic == "0TEG" and typ == 1:  # learn: the lookup answer for this camera
            TEMPLATES[self.uid] = bytes(data)
            CAMERA_IPS[self.uid] = self.cam_addr[0]
            save_learned()
        elif magic == "1TEG" and typ == 1 and len(data) >= 89:  # learn: the register/status answer
            TYPE1 = bytes(data)
            _token = int.from_bytes(TYPE1[85:89], "little")
            save_learned()
        log(f"RELAY real cloud {addr[0]}:{addr[1]} -> {self.uid}: {magic} type {typ} len {len(data)}")
        with open(RELAY_LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "dir": "cloud>cam", "server": f"{addr[0]}:{addr[1]}",
                                "uid": self.uid, "hex": masked(data)}) + "\n")


UPSTREAMS = {}


async def relay(port, data, cam_addr, uid):
    key = (uid, cam_addr)
    if key not in UPSTREAMS:
        loop = asyncio.get_running_loop()
        tr, _ = await loop.create_datagram_endpoint(lambda: Upstream(cam_addr, uid), local_addr=("0.0.0.0", 0))
        UPSTREAMS[key] = tr
    UPSTREAMS[key].sendto(data, REAL[port])
    magic, typ = kind_of(data)
    log(f"RELAY {uid} -> real cloud {REAL[port][0]}:{REAL[port][1]}: {magic} type {typ} len {len(data)}")
    with open(RELAY_LOG, "a") as f:
        f.write(json.dumps({"t": time.time(), "dir": "cam>cloud", "port": port, "uid": uid,
                            "hex": masked(data)}) + "\n")


def kind_of(data):
    magic = data[:4].decode("latin-1") if len(data) >= 4 else "?"
    typ = struct.unpack_from("<H", data, 4)[0] if len(data) >= 6 else None
    return magic, typ


class Cloud(asyncio.DatagramProtocol):
    def __init__(self, port):
        self.port = port
        self.counts = {}
        self.relay_addr = {}

    def connection_made(self, transport):
        self.t = transport
        PORTS[self.port] = self

    def sample(self, key, data, addr):
        n = self.counts.get(key, 0) + 1
        self.counts[key] = n
        if n <= SAMPLES_PER_KIND:
            with open(SAMPLES, "a") as f:
                f.write(json.dumps({"t": time.time(), "port": self.port, "from": addr[0], "sport": addr[1],
                                    "kind": key[1:], "hex": masked(data)}) + "\n")
        return n

    def datagram_received(self, data, addr):
        magic, typ = kind_of(data)
        key = (addr[0], magic, typ, len(data))
        n = self.sample(key, data, addr)
        if n == 1 or n % 100 == 0:
            log(f"UDP {self.port} {magic} type {typ} len {len(data)} from {addr[0]}:{addr[1]} (#{n})")
        ruid = relay_uid()
        if ruid:
            pkt_uid = None
            for off in (12, 32) if len(data) >= 44 else ():
                cand = data[off:off + 32].split(b"\0")[0].decode("latin-1")
                if cand == ruid:
                    pkt_uid = cand
            if pkt_uid or self.relay_addr.get(ruid) == addr or CAMERA_IPS.get(ruid) == addr[0]:
                self.relay_addr[ruid] = addr
                asyncio.ensure_future(relay(self.port, data, addr, ruid))
                return
        if self.port == 10102 and magic == "1TEG":
            if typ == 0 and len(data) >= 64:
                uid = data[32:64].split(b"\0")[0].decode("latin-1")
                UID_BY_IP[addr[0]] = uid
                reply = type1_for(uid, addr)
                if reply:
                    self.t.sendto(reply, addr)
            elif typ == 3:
                uid = UID_BY_IP.get(addr[0]) or next((u for u, ip in CAMERA_IPS.items() if ip == addr[0]), None)
                if uid:
                    reply = type1_for(uid, addr)
                if reply:
                    self.t.sendto(reply, addr)
            elif typ == 17:
                self.t.sendto(time_reply(), addr)
            return
        if self.port == 10101 and magic == "0TEG" and typ == 0 and len(data) >= 44:
            uid = data[12:44].split(b"\0")[0].decode("latin-1")
            reply = TEMPLATES.get(uid)
            if reply is None:
                log(f"0TEG lookup from unknown camera {uid!r}, not answered")
                return
            self.t.sendto(reply, addr)
            if n == 1 or n % 100 == 0:
                log(f"answered 0TEG lookup from {uid} ({addr[0]})")


async def main():
    loop = asyncio.get_running_loop()
    for port in (10101, 10102, 10103):
        await loop.create_datagram_endpoint(lambda p=port: Cloud(p), local_addr=(HOST_IP, port))
    log("iWFCam warm cloud listening on UDP 10101-10103")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
