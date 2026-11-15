"""Live view of what the cameras try to send home (caught by the fake clouds on the camera machine).

One SSH connection runs `tail -F` on the camera machine's fake-cloud logs; every line becomes an
event with a category and a plain explanation. Counts are exact even where a log
writes only every 100th packet: lines that carry a running number "(#N)" add the
difference to the previous number.

Sources on the camera machine:
  ~/bigeye-fakecloud.log                 360 camera (cloud stand-in)
  ~/.cache/bigeye-iwfcloud.log           door and window cameras (cloud stand-in)
  ~/bigeye-360-media/reports.jsonl       the 360 camera's own motion / person reports
"""
import collections
import json
import os
import re
import subprocess
import threading
import time

# The three logs, in this order: 360 stand-in cloud, door/window stand-in cloud, the 360's own reports.
KINDS = ("fake", "iwf", "report")
CAMERA_BY_IP = {}  # set by Telemetry(): camera Wi-Fi address -> camera name
CAMERA_NAMES = {"cam360": "Cam360", "door": "Door", "window": "Window (W2)",
                "door/window": "Door or window"}

# category -> (label, what the camera is trying to do)
CATEGORIES = {
    "upload": ("Picture upload", "Sends a picture from your room to its cloud storage (Alibaba OSS). Caught and kept on the camera machine."),
    "report": ("Motion report", "Tells the cloud it saw movement or a person, with time and picture name."),
    "uploadkey": ("Asks for upload keys", "Asks the cloud for storage keys so it can upload pictures and video."),
    "signin": ("Sign-in / status", "Signs in to its cloud, reports that it is bound to your account and its on/off switch."),
    "password": ("Sends its access password", "Sends the new password for live video it makes at every start. Whoever has it can watch."),
    "viewing": ("Viewing report", "Reports when and how long someone watched the live picture."),
    "firmware": ("Firmware / remote log check", "Asks for remote logging settings and firmware updates (a way to change the camera from afar)."),
    "keepalive": ("Keep-alive", "Keeps a permanent line open to the cloud so it can be reached and told what to do."),
    "p2p": ("P2P server (China/abroad)", "Says hello to the peer-to-peer servers that let the app (and the vendor) reach the camera from anywhere."),
    "relay": ("Video relay", "Tries the relay servers that carry live video out of the house."),
    "dns": ("Looks up a server", "Asks for the address of one of its cloud servers."),
    "time": ("Time and region", "Asks the cloud for the time, time zone and which server region to use."),
    "iwf": ("Door/window cloud", "The door or window camera calls its maker's cloud to say where it is."),
    "other": ("Other", "Other contact."),
}

LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (.*)$")
COUNTER = re.compile(r"\(#(\d+)\)")
IP = re.compile(r"from \(?'?(10\.42\.0\.\d+)")


def classify(text):
    t = text.lower()
    if t.startswith("upload put"):
        return "upload"
    if t.startswith("dns "):
        return "dns"
    if "socket.io" in t:
        return "keepalive"
    if "udp 32100" in t:
        return "p2p"
    if "udp 10000" in t or "udp 10126" in t:
        return "relay"
    if t.startswith("udp 1010"):
        return "iwf"
    if "/record/report" in t:
        return "report"
    if "/record/token" in t:
        return "uploadkey"
    if "/auth/password" in t:
        return "password"
    if "/auth/" in t:
        return "signin"
    if "/stats/" in t:
        return "viewing"
    if "/ota/" in t:
        return "firmware"
    if "/record/time" in t or "timezone" in t or "/service" in t or "httpdns" in t or "203.107.1.33" in t:
        return "time"
    return "other"


class Telemetry:
    def __init__(self, log, ssh_routes, logs, camera_by_ip):
        self.log = log
        self.routes = ssh_routes
        self.files = {os.path.basename(p): k for p, k in zip(logs, KINDS)}
        self.tail = "tail -n 30000 -F " + " ".join(logs)
        CAMERA_BY_IP.update(camera_by_ip)
        self.events = collections.deque(maxlen=40000)  # dicts: seq, t, cat, cam, text, n, bytes
        self.seq = 0
        self.lock = threading.Lock()
        self.last_counter = {}
        self.connected = False
        self.started = time.time()
        threading.Thread(target=self._run, daemon=True).start()

    def _add(self, t, cat, cam, text, n=1, size=0):
        with self.lock:
            self.seq += 1
            self.events.append({"seq": self.seq, "t": t, "cat": cat, "cam": cam, "text": text[:220], "n": n, "bytes": size})

    def _line(self, source, line):
        line = line.rstrip("\n")
        if not line:
            return
        if source == "report":
            try:
                r = json.loads(line)
            except ValueError:
                return
            what = {"body": "a PERSON", "motion": "movement"}.get(r.get("tag"), r.get("tag", "?"))
            self._add(float(r.get("received") or r.get("start_time") or time.time()), "report", "cam360",
                      f"Camera's own detection: {what} (picture {r.get('image_path', '?')}, account {r.get('user_id', '?')})")
            return
        m = LINE.match(line)
        if not m:
            return
        try:
            t = time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return
        text = m.group(2)
        if text.startswith(("fake cloud", "warm cloud", "relay ")):
            return  # our own start-up lines
        cat = classify(text)
        if source == "fake" and cat == "report":
            return  # counted from reports.jsonl, which says what it saw
        ipm = IP.search(text)
        if not ipm and source == "iwf":
            return  # the stand-in's own status lines, not a camera
        cam = CAMERA_BY_IP.get(ipm.group(1), ipm.group(1)) if ipm else ("cam360" if source == "fake" else "door/window")
        n = 1
        c = COUNTER.search(text)
        if c:  # the log writes every Nth packet with a running number
            key = (source, cat, cam, re.sub(r"\(#\d+\)|:\d+\)?$|len \d+", "", text)[:40])
            num, prev = int(c.group(1)), self.last_counter.get(key)
            n = num - prev if prev is not None and 0 < num - prev < 100000 else (num if prev is not None and num < prev else 1)
            self.last_counter[key] = num
        size = 0
        sm = re.search(r" (\d+) bytes", text)
        if sm:
            size = int(sm.group(1))
        self._add(t, cat, cam, text, n, size)

    def _run(self):
        while True:
            for route in self.routes:
                try:
                    proc = subprocess.Popen(route + [self.tail], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                            errors="replace")
                except OSError:
                    continue
                source, got = None, False
                for line in proc.stdout:
                    if not got:
                        got, self.connected = True, True
                        self.log("telemetry: following the fake-cloud logs on the camera machine")
                    if line.startswith("==> ") and line.rstrip().endswith(" <=="):
                        name = line.strip()[4:-4].rsplit("/", 1)[-1]
                        source = self.files.get(name)
                        continue
                    if source:
                        try:
                            self._line(source, line)
                        except Exception:
                            pass
                proc.wait()
                self.connected = False
                if got:
                    self.log("telemetry: lost the connection to the camera machine, reconnecting")
                    break
            time.sleep(10)

    def snapshot(self, since=0):
        now = time.time()
        lt = time.localtime(now)
        midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        with self.lock:
            evs = list(self.events)
        today = [e for e in evs if e["t"] >= midnight]
        hour = [e for e in evs if e["t"] >= now - 3600]
        bucket = 600  # 10 minutes
        start = (int(now) // bucket) * bucket - 71 * bucket  # last 12 hours
        bins = [0] * 72
        for e in evs:
            i = int((e["t"] - start) // bucket)
            if 0 <= i < 72:
                bins[i] += e["n"]
        cats = collections.Counter()
        cams = collections.Counter()
        split = collections.defaultdict(collections.Counter)  # category -> camera -> count
        cam_hour = collections.Counter()
        for e in today:
            cats[e["cat"]] += e["n"]
            cams[e["cam"]] += e["n"]
            split[e["cat"]][e["cam"]] += e["n"]
        for e in hour:
            cam_hour[e["cam"]] += e["n"]
        uploads = [e for e in today if e["cat"] == "upload"]
        return {
            "connected": self.connected,
            "now": now,
            "today": sum(e["n"] for e in today),
            "hour": sum(e["n"] for e in hour),
            "uploads": len(uploads),
            "upload_bytes": sum(e["bytes"] for e in uploads),
            "persons": sum(1 for e in today if e["cat"] == "report" and "PERSON" in e["text"]),
            "since_midnight_h": round((now - midnight) / 3600, 2),
            "bins": {"start": start, "size": bucket, "counts": bins},
            "categories": [{"key": k, "label": CATEGORIES[k][0], "why": CATEGORIES[k][1], "count": v,
                            "by_cam": dict(split[k].most_common())} for k, v in cats.most_common()],
            "cameras": dict(cams.most_common()),
            "cameras_hour": dict(cam_hour),
            "feed": sorted((e for e in evs if e["seq"] > since), key=lambda e: e["t"])[-120:],
            "seq": evs[-1]["seq"] if evs else 0,
            "labels": {k: v[0] for k, v in CATEGORIES.items()},
        }


# ---------------------------------------------------------------- report (one self-contained HTML file)

import base64
import html as _html

METHOD_STEPS = [
    ("Look up the vendor's servers", "At every start the 360 camera asks DNS for ep.tange365.com, device-we01.tange365.com, ihome-ota-service.tange365.com and more, and also uses a hard-coded HTTP DNS server (203.107.1.33) that bypasses the home's own DNS."),
    ("Ask where to go", "POST /service returns the region (we01) and the addresses of the sign-in, API and statistics servers (Alibaba cloud)."),
    ("Sign in and prove ownership", "/auth/active, /auth/get_bind_status, /auth/bind (first start); the owner's account id is included in every later report."),
    ("Hand over the key to the live video", "A new live-video password is generated at every start and sent to the cloud with /auth/password. Whoever holds the cloud can watch."),
    ("Keep a line open", "A permanent socket.io connection pings every 25 s, so the cloud can push orders (time zone, new servers, new storage keys) at any time."),
    ("Register with peer-to-peer servers", "UDP 32100 to servers in China (119.23.250.91, 47.107.255.39, 120.79.14.221) and abroad, plus relay ports 10000/10126 that carry live video out of the house."),
    ("Report what it sees and upload pictures", "The camera's own AI tags 'body' (person) or 'motion', sends /record/report, asks /record/token for real Alibaba OSS storage keys and uploads the picture over HTTPS."),
    ("Report viewing and ask for remote control", "/stats/device/streaming-record when someone watches; /ota/getlogconfig for remote logging and firmware updates."),
    ("Door and window cameras", "Do the same on UDP 10101-10103: they look themselves up and report where they are, so the app (and the vendor) can reach them."),
]


def _bar_svg(counts, start, size, w=900, h=180):
    n = len(counts) or 1
    top = max(4, max(counts) if counts else 4)
    bw, base = w / n, h - 20
    parts = [f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Attempts per time slot">']
    for g in range(5):
        y = 4 + (base - 4) * (1 - g / 4)
        parts.append(f'<line x1="0" x2="{w}" y1="{y:.1f}" y2="{y:.1f}" stroke="#e3e6ea"/>'
                     f'<text x="2" y="{y - 2:.1f}" font-size="9" fill="#6b7280">{round(top * g / 4)}</text>')
    for i, c in enumerate(counts):
        bh = (base - 4) * c / top
        if c:
            parts.append(f'<rect x="{i * bw + 1:.1f}" y="{base - bh:.1f}" width="{max(1, bw - 2):.1f}" height="{bh:.1f}" rx="2" fill="#1f6fd1"><title>{c}</title></rect>')
        if i % max(1, n // 8) == 0:
            parts.append(f'<text x="{i * bw:.1f}" y="{h - 5}" font-size="9" fill="#6b7280">{time.strftime("%H:%M", time.localtime(start + i * size))}</text>')
    parts.append("</svg>")
    return "".join(parts)


def build_report(tel, period, fetch_picture=None, pictures=None):
    """period: 'today' or '24h'. fetch_picture(file) -> bytes; pictures: list of {file, time, size}."""
    now = time.time()
    lt = time.localtime(now)
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    start = midnight if period == "today" else now - 86400
    with tel.lock:
        evs = [e for e in tel.events if e["t"] >= start]
    total = sum(e["n"] for e in evs)
    hours = max(0.1, (now - start) / 3600)
    cats, cams = collections.Counter(), collections.Counter()
    split = collections.defaultdict(collections.Counter)
    for e in evs:
        cats[e["cat"]] += e["n"]
        cams[e["cam"]] += e["n"]
        split[e["cat"]][e["cam"]] += e["n"]
    size = 1800 if period == "24h" else 900
    nbins = int((now - start) // size) + 1
    bins = [0] * nbins
    for e in evs:
        bins[min(nbins - 1, int((e["t"] - start) // size))] += e["n"]
    uploads = [e for e in evs if e["cat"] == "upload"]
    persons = sum(1 for e in evs if e["cat"] == "report" and "PERSON" in e["text"])
    passwords = [e for e in evs if e["cat"] == "password"]
    busiest = max(range(nbins), key=lambda i: bins[i]) if evs else 0
    esc = _html.escape
    period_txt = f'{time.strftime("%Y-%m-%d %H:%M", time.localtime(start))} - {time.strftime("%Y-%m-%d %H:%M", lt)}'

    rows = "".join(f"<tr><td><b>{esc(CATEGORIES[k][0])}</b><br><span class=d>{esc(CATEGORIES[k][1])}</span></td>"
                   f"<td>{esc(' · '.join(f'{c} {n:,}' for c, n in split[k].most_common()))}</td>"
                   f"<td class=n>{v:,}</td><td class=n>{v / hours:,.0f}</td><td class=n>{100 * v / max(1, total):.1f} %</td></tr>"
                   for k, v in cats.most_common())
    camrows = "".join(f"<tr><td><b>{esc(CAMERA_NAMES.get(c, str(c)))}</b><br><span class=d>top: "
                      f"{esc(', '.join(CATEGORIES[k][0].lower() for k, _ in collections.Counter({k: split[k][c] for k in split if split[k][c]}).most_common(3)))}</span></td>"
                      f"<td class=n>{v:,}</td><td class=n>{v / hours:,.0f}</td><td class=n>{100 * v / max(1, total):.1f} %</td></tr>" for c, v in cams.most_common())
    notable = [f"Busiest {size // 60} minutes: {time.strftime('%H:%M', time.localtime(start + busiest * size))} with {bins[busiest]:,} attempts."]
    notable.append(f"The 360 camera sent its live-video password {len(passwords)} time(s)"
                   + (f" (at {', '.join(time.strftime('%H:%M', time.localtime(e['t'])) for e in passwords)}), which means it restarted that often." if passwords else "."))
    notable.append(f"It tried to upload {len(uploads)} pictures ({sum(e['bytes'] for e in uploads) / 1024:,.0f} KB) and sent {persons} 'person seen' reports.")
    notable.append(f"Peer-to-peer server contacts (China/abroad): {cats.get('p2p', 0):,}; permanent-line keep-alives: {cats.get('keepalive', 0):,}.")

    pics_html = ""
    if fetch_picture and pictures:
        chosen = [p for p in pictures if p.get("time", 0) >= start][:6]
        cells = []
        for p in chosen:
            try:
                data = base64.b64encode(fetch_picture(p["file"])).decode()
                cells.append(f'<figure><img src="data:image/jpeg;base64,{data}" alt="caught upload">'
                             f'<figcaption>{time.strftime("%H:%M:%S", time.localtime(p["time"]))} · {p.get("size", 0) // 1024} KB</figcaption></figure>')
            except Exception:
                continue
        if cells:
            pics_html = ("<h2>Pictures the camera tried to send out</h2><p class=d>Caught by the stand-in cloud on the camera machine; none of them left the house.</p>"
                         f'<div class=pics>{"".join(cells)}</div>')
    steps = "".join(f"<li><b>{esc(a)}.</b> {esc(b)}</li>" for a, b in METHOD_STEPS)
    return f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Call-home Report</title>
<style>
:root{{--txt:#1b1f24;--dim:#5a6270;--line:#dfe3e8;--acc:#1f6fd1}}
body{{margin:0;background:#fff;color:var(--txt);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
.page{{max-width:900px;margin:0 auto;padding:32px 24px}}h1{{font-size:24px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}}
.d{{color:var(--dim);font-size:12px}}.tiles{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
.tile{{border:1px solid var(--line);border-radius:10px;padding:10px 12px}}.tile b{{display:block;font-size:24px}}.tile span{{color:var(--dim);font-size:12px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}td,th{{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}}.n{{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}}
ul,ol{{padding-left:20px}}li{{margin:4px 0}}.pics{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}}figure{{margin:0}}figure img{{width:100%;border-radius:6px;display:block}}figcaption{{color:var(--dim);font-size:11px}}
.bar{{position:sticky;top:0;background:#f5f6f8;border-bottom:1px solid var(--line);padding:8px 24px;display:flex;gap:8px}}.bar button{{font:inherit;padding:6px 12px;border-radius:8px;border:1px solid var(--line);background:#fff;cursor:pointer}}
@media print{{.bar{{display:none}}.page{{padding:0}}h2{{break-after:avoid}}tr,figure{{break-inside:avoid}}}}
@media (max-width:640px){{.tiles{{grid-template-columns:repeat(2,1fr)}}.pics{{grid-template-columns:repeat(2,1fr)}}}}
</style>
<div class="bar"><button onclick="print()">Print / save as PDF</button><span class="d" style="align-self:center">A copy of this report is saved on this PC.</span></div>
<div class="page">
<h1>Camera call-home report</h1>
<div class="d">Sentinel-BigEye · period {esc(period_txt)} · generated {time.strftime("%Y-%m-%d %H:%M:%S", lt)}</div>
<div class="tiles">
<div class="tile"><b>{total:,}</b><span>attempts to call home</span></div>
<div class="tile"><b>{total / hours:,.0f}</b><span>per hour on average</span></div>
<div class="tile"><b>{len(uploads)}</b><span>pictures it tried to upload</span></div>
<div class="tile"><b>{persons}</b><span>"person seen" reports</span></div>
</div>
<h2>Summary</h2><ul>{"".join(f"<li>{esc(x)}</li>" for x in notable)}</ul>
<h2>Attempts over time</h2><p class="d">Per {size // 60} minutes, all cameras.</p>{_bar_svg(bins, start, size)}
<h2>Per camera</h2><table><tr><th>Camera</th><th class="n">Attempts</th><th class="n">Per hour</th><th class="n">Share</th></tr>{camrows}</table>
<h2>What they tried to do</h2><table><tr><th>Kind</th><th>Which camera</th><th class="n">Attempts</th><th class="n">Per hour</th><th class="n">Share</th></tr>{rows}</table>
{pics_html}
<h2>How they phone home</h2><ol>{steps}</ol>
<h2>How it was stopped</h2>
<p>The cameras sit on an isolated Wi-Fi on a separate machine (the camera machine) with no way out to the internet or the home network. Their DNS and cloud ports are redirected to local stand-ins that answer like the vendor's cloud and keep what they receive. Live video, recording, motion and person detection run locally on the owner's own machines. Counts are exact even where a log records only every 20th or 100th packet: the running counters in the logs are added up.</p>
<p class="d">Source: stand-in cloud logs on the camera machine (bigeye-fakecloud, bigeye-iwfcloud, the 360 camera's own reports), read live by Sentinel-BigEye.</p>
</div></html>"""
