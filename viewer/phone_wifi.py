"""Wi-Fi on the phone cameras (BigEye Cam): which network a phone is on, which networks it can see, and moving it
to another one from the BigEye page.

Everything goes over ADB (wireless debugging, `adb tcpip 5555`): straight from this PC when the phone is on the home
Wi-Fi, or through the camera machine when it is on the camera Wi-Fi. A password typed on the page is handed to the phone once and
never stored by BigEye.

Two kinds of phone:
  * Android 11+ (A15): `cmd wifi` can scan and connect. A small script runs on the phone itself, so it finishes even
    when the switch cuts this PC off. If the new network does not work within ~2.5 minutes and the phone did not know it
    before, the script forgets it again and the phone falls back to a network it already knew.
  * Older Android (J3, 5.1): no `cmd wifi`. BigEye opens the Wi-Fi settings, reads the screen (uiautomator) and taps
    the network and its Connect button, like a person would.
"""
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import tempfile
import urllib.request
import xml.etree.ElementTree as ET

ADB = os.environ.get("BIGEYE_ADB", "/mnt/c/platform-tools/adb.exe")   # Windows adb from WSL (it sees the home Wi-Fi)
REMOTE_ADB = "~/platform-tools/adb"
PHONE_SCRIPT = "/data/local/tmp/bigeye-wifi.sh"
CONNECT_WORDS = ("koble til", "connect", "join", "bli med")
_SDK = {}
# Wake the old phone and lift its swipe lock (no PIN on the camera phones), so its screen can be read and tapped.
UNLOCK = "input keyevent KEYCODE_WAKEUP; input keyevent 82; input swipe 360 1000 360 200 300"


class Phone:
    """Where a phone is reached: its address and the way there ("lan" = from this PC, "pc2" = through the camera machine)."""

    def __init__(self, ip, via, ssh_routes=()):
        self.ip, self.via, self.ssh_routes = ip, via, list(ssh_routes)
        self.serial = f"{ip}:5555"

    # ------------------------------------------------------------ plumbing
    def _local(self, args, timeout, text_in=None):
        cwd = "/mnt/c" if ADB.startswith("/mnt/") else None   # Windows tools refuse a WSL folder as working folder
        out = subprocess.run([ADB] + args, capture_output=True, text=True, timeout=timeout, cwd=cwd, input=text_in)
        return (out.stdout + out.stderr).replace("\r", "")

    def _pc2(self, shell_line, timeout, text_in=None):
        last = ""
        for route in self.ssh_routes:
            try:
                out = subprocess.run(route + [shell_line], capture_output=True, text=True, timeout=timeout, input=text_in)
            except subprocess.TimeoutExpired:
                last = "timed out"
                continue
            if out.returncode != 255:   # 255 = ssh itself failed: try the next way to the camera machine
                return (out.stdout + out.stderr).replace("\r", "")
            last = out.stderr
        raise RuntimeError(f"the camera machine not reachable: {last.strip()[:120]}")

    def adb(self, *args, timeout=30):
        if self.via == "pc2":
            q = " ".join(shlex.quote(a) for a in args)
            return self._pc2(f"{REMOTE_ADB} connect {self.serial} >/dev/null 2>&1; {REMOTE_ADB} -s {self.serial} {q}", timeout)
        self._local(["connect", self.serial], 10)
        return self._local(["-s", self.serial] + list(args), timeout)

    def shell(self, line, timeout=30):
        """A fixed command line on the phone (never anything typed by a person: that goes through push_script)."""
        return self.adb("shell", line, timeout=timeout)

    def push_script(self, text):
        """Put a script on the phone without it ever being on a command line (it may hold a password)."""
        if self.via == "pc2":
            self._pc2(f"f=$(mktemp); cat > \"$f\"; {REMOTE_ADB} -s {self.serial} push \"$f\" {PHONE_SCRIPT} >/dev/null; rm -f \"$f\"",
                      30, text_in=text)
            return
        folder = os.path.expanduser("~/.cache/bigeye-wifi")
        os.makedirs(folder, mode=0o700, exist_ok=True)
        fd, path = tempfile.mkstemp(dir=folder, suffix=".sh")
        try:
            with os.fdopen(fd, "w", newline="\n") as fh:
                fh.write(text)
            src = path
            if ADB.endswith(".exe"):   # Windows adb reads the WSL file through \\wsl.localhost\...
                src = subprocess.run(["wslpath", "-w", path], capture_output=True, text=True).stdout.strip()
            self._local(["connect", self.serial], 10)
            self._local(["-s", self.serial, "push", src, PHONE_SCRIPT], 30)
        finally:
            os.unlink(path)

    def sdk(self):
        if self.ip not in _SDK:
            out = self.shell("getprop ro.build.version.sdk", 15).strip().splitlines()
            try:
                _SDK[self.ip] = int(out[-1])
            except (IndexError, ValueError):
                raise RuntimeError("the phone does not answer ADB (wireless debugging off?)")
        return _SDK[self.ip]

    # ------------------------------------------------------------ reading
    def status(self):
        if self.sdk() >= 30:
            line = next((l for l in self.shell("cmd wifi status").splitlines() if "SSID:" in l), "")
            ssid = re.search(r'SSID: "(.*?)"', line)
        else:
            line = next((l for l in self.shell("dumpsys wifi", 40).splitlines() if l.startswith("mWifiInfo")), "")
            ssid = re.search(r"SSID: (.*?), BSSID", line)
        rssi = re.search(r"RSSI: (-?\d+)", line)
        freq = re.search(r"Frequency: (\d+)", line)
        return {"ssid": ssid.group(1) if ssid else None, "rssi": int(rssi.group(1)) if rssi else None,
                "band": band(int(freq.group(1))) if freq else None, "ip": self.ip, "via": self.via}

    def saved(self):
        if self.sdk() >= 30:
            names = []
            for l in self.shell("cmd wifi list-networks").splitlines()[1:]:
                cols = re.split(r"\s{2,}", l.strip())
                if len(cols) >= 3 and cols[0].isdigit():
                    names.append(cols[1])
            return sorted(set(names))
        dump = self.shell("dumpsys wifi", 40)
        part = dump.split("Configured networks", 1)[-1]
        return sorted(set(re.findall(r'^\*? ?ID: \d+ SSID: "(.*?)"', part, re.M)))

    def scan(self):
        if self.sdk() >= 30:
            out = self.shell("cmd wifi start-scan; sleep 4; cmd wifi list-scan-results", 40)
        else:   # the old phone only scans while its Wi-Fi settings are open
            out = self.shell(f"{UNLOCK}; am start -a android.settings.WIFI_SETTINGS >/dev/null; sleep 8; dumpsys wifi; "
                             "input keyevent KEYCODE_HOME", 60)
            rows = []
            for l in out.split("Latest scan results:", 1)[-1].splitlines():
                if l.strip() and not l.startswith(" "):
                    break   # the next section of the dump
                rows.append(l)
            out = "\n".join(rows)
        best = {}
        for l in out.splitlines():
            m = re.match(r"^\s*((?:[0-9a-f]{2}:){5}[0-9a-f]{2})\s+(\d+)\s+(-?\d+)\s+\S+\s+(.*?)\s*(\[.*\])?\s*$", l)
            if not m or not m.group(4).strip():
                continue
            ssid, freq, rssi, flags = m.group(4).strip(), int(m.group(2)), int(m.group(3)), m.group(5) or ""
            if ssid not in best or rssi > best[ssid]["rssi"]:
                best[ssid] = {"ssid": ssid, "rssi": rssi, "band": band(freq), "security": security(flags)}
        return sorted(best.values(), key=lambda n: -n["rssi"])

    # ------------------------------------------------------------ switching
    def connect(self, ssid, password, sec, known):
        """Start the switch. Returns at once; the phone may drop off this PC's reach a few seconds later."""
        if self.sdk() >= 30:
            return self._connect_cmd(ssid, password, sec, known)
        return self._connect_taps(ssid, password)

    def _connect_cmd(self, ssid, password, sec, known):
        if sec != "open" and not password:
            raise ValueError("this network needs its password (BigEye never stores passwords)")
        args = f"{shlex.quote(ssid)} {sec}" + (f" {shlex.quote(password)}" if sec != "open" else "")
        script = f"""rm -f {PHONE_SCRIPT}
S={shlex.quote(ssid)}
cmd wifi connect-network {args} >/dev/null 2>&1
i=0
while [ $i -lt 150 ]; do
  sleep 5; i=$((i+5))
  case "$(cmd wifi status)" in *"SSID: \\"$S\\""*"IP: /"[0-9]*) exit 0;; esac
done
# never got on: forget it again (only if it was new), so the phone goes back to a network it knew
if [ {0 if known else 1} = 1 ]; then
  cmd wifi list-networks | grep -F "  $S  " | while read -r id rest; do cmd wifi forget-network "$id"; done
fi
"""
        self.push_script(script)
        self.shell(f"setsid nohup sh {PHONE_SCRIPT} >/dev/null 2>&1 </dev/null &", 15)
        return "switching"

    def _screen(self):
        xml = self.shell("uiautomator dump /sdcard/bigeye-ui.xml >/dev/null 2>&1; cat /sdcard/bigeye-ui.xml", 40)
        xml = xml[xml.find("<?xml"):] if "<?xml" in xml else xml[xml.find("<hierarchy"):]
        try:
            return ET.fromstring(xml)
        except ET.ParseError:
            raise RuntimeError("could not read the phone's screen")

    @staticmethod
    def _centre(node):
        x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.get("bounds", "")))
        return (x1 + x2) // 2, (y1 + y2) // 2

    def _connect_taps(self, ssid, password):
        self.shell(f"{UNLOCK}; am start -a android.settings.WIFI_SETTINGS >/dev/null; sleep 6")
        hit = next((n for n in self._screen().iter("node") if n.get("text") == ssid), None)
        if hit is None:
            self.shell("input keyevent KEYCODE_HOME")
            raise RuntimeError(f"{ssid} is not in the phone's list right now")
        x, y = self._centre(hit)
        self.shell(f"input tap {x} {y}; sleep 2")
        root = self._screen()
        field = next((n for n in root.iter("node") if n.get("class") == "android.widget.EditText"), None)
        if field is not None:
            if not password:
                self.shell("input keyevent KEYCODE_BACK; input keyevent KEYCODE_HOME")
                raise ValueError("this network needs its password (BigEye never stores passwords)")
            fx, fy = self._centre(field)
            typed = "".join("%s" if ch == " " else ch for ch in password)   # `input text` turns %s into a space
            self.push_script(f"rm -f {PHONE_SCRIPT}\ninput tap {fx} {fy}\ninput text {shlex.quote(typed)}\n")
            self.shell(f"sh {PHONE_SCRIPT}")
            root = self._screen()
        button = next((n for n in root.iter("node")
                       if (n.get("text") or "").strip().lower() in CONNECT_WORDS and n.get("enabled") != "false"), None)
        if button is not None:
            bx, by = self._centre(button)
            self.shell(f"input tap {bx} {by}; input keyevent KEYCODE_HOME")
        else:   # a network the phone knew connects on the first tap
            self.shell("input keyevent KEYCODE_HOME")
        return "switching"


def band(freq):
    return "5 GHz" if freq > 4000 else "2.4 GHz"


def security(flags):
    if "SAE" in flags and "PSK" not in flags:
        return "wpa3"
    if "PSK" in flags:
        return "wpa2"
    if "WEP" in flags:
        return "wep"
    if "OWE" in flags:
        return "owe"
    return "open"


def _is_phone(url, model):
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            st = json.load(r)
        return st.get("app") == "BigEye Cam" and st.get("model") == model
    except Exception:
        return False


def find_phone(model, home_prefix, ssh_routes):
    """Look for the BigEye Cam phone of this model: first on the home Wi-Fi, then on the camera Wi-Fi behind the camera machine."""
    if home_prefix:
        urls = {f"http://{home_prefix}{i}:8080/status": f"{home_prefix}{i}" for i in range(1, 255)}
        with concurrent.futures.ThreadPoolExecutor(64) as pool:
            for url, ok in zip(urls, pool.map(lambda u: _is_phone(u, model), urls)):
                if ok:
                    return urls[url], "lan"
    line = ('for i in $(seq 2 254); do (curl -s -m2 http://10.42.0.$i:8080/status | grep -q '
            + shlex.quote(f'"model":"{model}"') + ' && echo 10.42.0.$i) & done; wait')
    for route in ssh_routes:
        try:
            out = subprocess.run(route + [line], capture_output=True, text=True, timeout=40).stdout.split()
        except Exception:
            continue
        if out:
            return out[0], "pc2"
    return None, None
