#!/usr/bin/env python3
"""A stand-in for the Tange (iCam365) cloud, so the 360 camera stays calm on the
dead camera network. Runs on the camera machine; the firewall sends the camera's DNS, HTTP and
P2P-server traffic here. Answers mirror what the real cloud answered on
2026-11-13, except that no real credentials are ever handed out.

  UDP 15353  DNS: every A query -> 10.42.0.1
  TCP 18080  HTTP API + socket.io (engine.io v4 over websocket)
  UDP 32100  CS2/PPPP server: HELLO -> HELLO_ACK, login -> replayed ACK
  UDP 10000, 10126  relay ports: logged only
Log: ~/bigeye-fakecloud.log
"""
import asyncio, base64, hashlib, json, os, ssl, struct, subprocess, time

HOST_IP = os.environ.get("BIGEYE_HOST_IP", "10.42.0.1")  # this machine on the camera Wi-Fi
# Names resolve to an address outside the camera network (TEST-NET-3), so the
# firewall's "not local" redirect rules catch the traffic and send it here.
FAKE_IP = "203.0.113.10"
LOG = os.path.expanduser("~/bigeye-fakecloud.log")
DEV_LGN_ACK = bytes.fromhex(os.environ.get("BIGEYE_360_LOGIN_ACK", ""))  # optional: a P2P login answer you captured yourself
UUID = os.environ.get("BIGEYE_360_UUID", "")  # the ID printed on the camera's label
MEDIA_DIR = os.path.expanduser("~/bigeye-360-media")
KEEP_DAYS = 14


def save_upload(key, body, ctype):
    """Keep the camera's own event pictures/clips (what it would upload to its cloud)."""
    if ctype.startswith("multipart/form-data") and "boundary=" in ctype:
        boundary = ("--" + ctype.split("boundary=", 1)[1].strip('"')).encode()
        for part in body.split(boundary):
            if b"filename=" in part and b"\r\n\r\n" in part:
                body = part.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n-")
                break
    day = time.strftime("%Y-%m-%d")
    name = "".join(c if c.isalnum() or c in "._-" else "_" for c in key.strip("/").replace("/", "_"))[-120:] or "upload"
    if "." not in name[-6:]:
        name += ".jpg" if body[:3] == b"\xff\xd8\xff" else ".bin"
    folder = os.path.join(MEDIA_DIR, day)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("%H%M%S-") + name)
    with open(path, "wb") as handle:
        handle.write(body)
    prune()
    return path


def save_report(body):
    os.makedirs(MEDIA_DIR, exist_ok=True)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        data = {"raw": body[:500].decode("latin1")}
    data["received"] = int(time.time())
    with open(os.path.join(MEDIA_DIR, "reports.jsonl"), "a") as handle:
        handle.write(json.dumps(data) + "\n")


def prune():
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - KEEP_DAYS * 86400))
    for day in os.listdir(MEDIA_DIR):
        full = os.path.join(MEDIA_DIR, day)
        if os.path.isdir(full) and day < cutoff:
            for f in os.listdir(full):
                os.remove(os.path.join(full, f))
            os.rmdir(full)


def media_list(limit=60):
    items = []
    if os.path.isdir(MEDIA_DIR):
        for day in sorted(os.listdir(MEDIA_DIR), reverse=True):
            full = os.path.join(MEDIA_DIR, day)
            if not os.path.isdir(full):
                continue
            for f in sorted(os.listdir(full), reverse=True):
                st = os.stat(os.path.join(full, f))
                items.append({"file": f"{day}/{f}", "size": st.st_size, "time": int(st.st_mtime)})
                if len(items) >= limit:
                    return items
    return items


def log(msg):
    with open(LOG, "a") as handle:
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def ok(data=None, **extra):
    body = {"code": 200, "ok": 1, "status": "ok"}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return body


def api_answer(path, body):
    now = int(time.time())
    if path == "/service":
        return ok({"reg": "ws://we01-reg-node001.tange365.com/socket.io/", "api": "http://device-we01.tange365.com/",
                   "ai": "http://device-we01.tange365.com/ai/", "stat": "http://stat-we01.tange365.com/",
                   "region": "we01", "http_dns": {}, "p2p": {"url": "", "ttl": 0}})
    if path == "/record/time":
        return ok(now)
    if path == "/app/device/timezone/get":
        return ok({"tz": "GMT+01:00"})
    if path == "/auth/active":
        return ok("active success")
    if path == "/auth/get_bind_status":
        return ok({"is_bind": 1})
    if path == "/auth/get_bind_params":
        # The camera's own P2P settings, as its real cloud returned them (kept privately
        # on the camera machine). Without them the camera never starts its P2P service.
        try:
            with open(os.path.expanduser("~/.config/sentinel-bigeye/cam360-bind.json")) as handle:
                return ok(json.load(handle))
        except (OSError, ValueError):
            return ok({})
    if path == "/auth/password":
        # The camera makes a new local P2P password at every boot and reports it here.
        # Keep the latest one privately; the local video bridge logs in with it.
        try:
            password = json.loads(body or b"{}").get("password")
            if password:
                target = os.path.expanduser("~/.config/sentinel-bigeye/cam360-pass")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target + ".tmp", "w") as handle:
                    handle.write(password)
                os.chmod(target + ".tmp", 0o600)
                os.replace(target + ".tmp", target)
                log("camera reported a new local password (stored)")
        except ValueError:
            pass
        return ok("")
    if path == "/auth/switch":
        return ok("")
    if path == "/auth/service_list":
        return ok({"serviceData": [], "aiServiceData": [], "wxVoipServiceData": []})
    if path == "/record/token":
        # Deliberately useless credentials: nothing can be uploaded anywhere.
        # The upload host (<bucket>.<endpoint>) resolves to this fake cloud, which
        # accepts and discards the images, so failed uploads do not upset the camera.
        return ok({"from": "aliyun", "endPoint": "http://oss.bigeye.local", "AccessKeyId": "local", "AccessKeySecret": "local",
                   "SecurityToken": "local", "Expiration": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 43200)),
                   "Expiration_int": now + 43200, "bucket": "bucket", "rootPath": UUID, "ossid": "111",
                   "region_id": "local"})
    if path.startswith("/record/report"):
        save_report(body)
        return ok("succeed")
    if path.startswith("/stats/"):
        return ok("succeed")
    return ok({})


def http_dns_answer(path):
    return {"host": "we01-reg-node001.tange365.com", "ips": [FAKE_IP], "ttl": 600, "origin_ttl": 600, "client_ip": HOST_IP}


# ---------------------------------------------------------------- websocket (socket.io)

def ws_frame(text):
    data = text.encode()
    head = bytes([0x81])
    if len(data) < 126:
        head += bytes([len(data)])
    else:
        head += bytes([126]) + struct.pack(">H", len(data))
    return head + data


async def ws_read(reader):
    b0, b1 = await reader.readexactly(2)
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", await reader.readexactly(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", await reader.readexactly(8))[0]
    key = await reader.readexactly(4) if b1 & 0x80 else b"\0\0\0\0"
    data = await reader.readexactly(ln)
    return b0 & 0x0F, bytes(x ^ key[i % 4] for i, x in enumerate(data))


async def socketio(reader, writer, peer):
    sid = base64.urlsafe_b64encode(os.urandom(15)).decode()[:20]
    writer.write(ws_frame('0' + json.dumps({"sid": sid, "pingInterval": 25000, "pingTimeout": 50000, "upgrades": []})))
    writer.write(ws_frame("40"))
    for event, payload in ((f"tg/device/{UUID}/timezone", {"device_id": 0, "uuid": UUID, "timezone": "GMT+01:00", "is_log": 0}),
                           (f"tg/device/{UUID}/service", {"serviceData": [], "aiServiceData": [], "wxVoipServiceData": []})):
        writer.write(ws_frame("42" + json.dumps([event, payload])))
    await writer.drain()
    log(f"socket.io connected from {peer}")
    pings = 0
    while True:
        op, data = await ws_read(reader)
        if op == 8:
            break
        if op == 9:
            writer.write(bytes([0x8A, len(data)]) + data)
        elif data == b"2":
            writer.write(ws_frame("3"))
            pings += 1
            if pings % 20 == 1:
                log(f"socket.io ping from {peer} (#{pings})")
        else:
            log(f"socket.io message from {peer}: {data[:200]!r}")
        await writer.drain()
    log(f"socket.io closed from {peer} after {pings} pings")


async def handle_http(reader, writer):
    peer = writer.get_extra_info("peername")[0]
    try:
        while True:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin1").split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
            if headers.get("transfer-encoding", "").lower() == "chunked":
                body = b""
                while True:
                    size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
                    if size == 0:
                        await reader.readline()
                        break
                    body += await reader.readexactly(size)
                    await reader.readline()
            else:
                body = await reader.readexactly(int(headers.get("content-length", "0") or 0))
            path = target.split("?")[0]
            host = headers.get("host", "?")
            if method == "GET" and path.startswith("/bigeye/"):
                # Local-only view of what the camera uploaded (reached through an SSH tunnel).
                if path == "/bigeye/list":
                    data = json.dumps(media_list()).encode()
                    ctype = "application/json"
                else:
                    rel = os.path.normpath(path[len("/bigeye/file/"):]).lstrip("/")
                    full = os.path.join(MEDIA_DIR, rel)
                    if rel.startswith("..") or not os.path.isfile(full):
                        writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                        await writer.drain()
                        continue
                    data = open(full, "rb").read()
                    ctype = "image/jpeg" if full.endswith(".jpg") else "application/octet-stream"
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(data)}\r\n"
                             "Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n".encode() + data)
                await writer.drain()
                continue
            if method in ("PUT", "POST") and ("oss.bigeye.local" in host or path.startswith("/" + UUID)):
                # Event picture/clip meant for the vendor's cloud storage: keep it here instead.
                saved = save_upload(path, body, headers.get("content-type", ""))
                log(f"UPLOAD {method} {host}{path[:80]} {len(body)} bytes from {peer} -> {saved}")
                writer.write(b"HTTP/1.1 200 OK\r\nETag: \"00000000000000000000000000000000\"\r\n"
                             b"x-oss-request-id: local\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n")
                await writer.drain()
                continue
            if headers.get("expect", "").lower() == "100-continue":
                pass
            if path.startswith("/socket.io") and headers.get("upgrade", "").lower() == "websocket":
                accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                              f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                await writer.drain()
                await socketio(reader, writer, peer)
                return
            answer = http_dns_answer(path) if path.endswith("/d") and path.count("/") == 2 else api_answer(path, body)
            data = json.dumps(answer, separators=(",", ":")).encode()
            log(f"HTTP {method} {headers.get('host', '?')}{path} from {peer}")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
                         + f"Content-Length: {len(data)}\r\nConnection: keep-alive\r\n\r\n".encode() + data)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionError, ValueError):
        pass
    finally:
        writer.close()


# ---------------------------------------------------------------- UDP services

class DNS(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        if len(data) < 12:
            return
        qd = data[12:]
        i, labels = 0, []
        while i < len(qd) and qd[i]:
            labels.append(qd[i + 1:i + 1 + qd[i]].decode("latin1"))
            i += qd[i] + 1
        qtype = struct.unpack(">H", qd[i + 1:i + 3])[0] if len(qd) >= i + 3 else 1
        question = qd[:i + 5]
        name = ".".join(labels)
        log(f"DNS {name} type {qtype} from {addr[0]}")
        header = data[:2] + b"\x81\x80" + data[4:6] + (b"\x00\x01" if qtype == 1 else b"\x00\x00") + b"\x00\x00\x00\x00"
        answer = (b"\xc0\x0c\x00\x01\x00\x01" + struct.pack(">IH", 60, 4) + bytes(map(int, FAKE_IP.split(".")))) if qtype == 1 else b""
        self.t.sendto(header + question + answer, addr)


class PPPPServer(asyncio.DatagramProtocol):
    def __init__(self, port):
        self.port = port
        self.counts = {}

    def connection_made(self, transport):
        self.t = transport

    def datagram_received(self, data, addr):
        kind = data[1] if len(data) > 1 and data[0] == 0xF1 else None
        key = (self.port, kind)
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.counts[key] in (1, 10, 100) or self.counts[key] % 500 == 0:
            log(f"UDP {self.port} type {kind if kind is None else hex(kind)} len {len(data)} from {addr} (#{self.counts[key]})")
        if self.port != 32100 or kind is None:
            return
        if kind == 0x00:  # HELLO -> HELLO_ACK with the sender's address (ip bytes reversed, as the real server did)
            ip = bytes(map(int, addr[0].split(".")))[::-1]
            body = b"\x00\x02" + struct.pack(">H", addr[1]) + ip + b"\x00" * 8
            self.t.sendto(b"\xf1\x01" + struct.pack(">H", len(body)) + body, addr)
        elif kind == 0x12:  # device login -> replay a captured ACK
            if DEV_LGN_ACK:
                self.t.sendto(DEV_LGN_ACK, addr)


async def main():
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(DNS, local_addr=(HOST_IP, 15353))
    for port in (32100, 10000, 10126):
        await loop.create_datagram_endpoint(lambda p=port: PPPPServer(p), local_addr=(HOST_IP, port))
    server = await asyncio.start_server(handle_http, HOST_IP, 18080)
    # HTTPS on 18443 (the firewall sends the camera's port 443 here) with a
    # self-made certificate, for uploads the camera insists on encrypting.
    cert = os.path.expanduser("~/.config/sentinel-bigeye/fakecloud.pem")
    if not os.path.exists(cert):
        os.makedirs(os.path.dirname(cert), exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
                        "-subj", "/CN=*.tange365.com", "-keyout", cert, "-out", cert], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.chmod(cert, 0o600)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.set_ciphers("ALL:@SECLEVEL=0")
    ctx.load_cert_chain(cert)
    tls = await asyncio.start_server(handle_http, HOST_IP, 18443, ssl=ctx, ssl_handshake_timeout=10)
    log("fake cloud started")
    async with server, tls:
        await asyncio.gather(server.serve_forever(), tls.serve_forever())


if __name__ == "__main__":
    asyncio.run(main())
