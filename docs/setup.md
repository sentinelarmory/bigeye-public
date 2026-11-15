# Setup

Two roles, which can be the same Linux box:

- **camera machine**: hosts the camera Wi-Fi, the firewall, the bridges and the stand-in clouds
- **main machine**: runs the page and the AI (WSL works; the folder buttons then open Windows Explorer)

Each step ends with a **check**. Do not go on until it passes.

## 1. Camera Wi-Fi with no way out (camera machine)

1. Create a hotspot on the spare Wi-Fi card, for example with NetworkManager:
   `nmcli dev wifi hotspot ifname wlan0 ssid BigEye-cams password '<long password>'` (address 10.42.0.1/24).
2. Edit `camera-machine/bigeye-firewall.nft`: replace the placeholder MACs (360 camera, door/window cameras, the PC
   allowed to SSH in) and the interface name if it is not `wlp5s0`.
3. Install it: `sudo bash camera-machine/bigeye-firewall.sh` (undo: `sudo bash camera-machine/bigeye-firewall.sh remove`).

**Check:** a phone on the camera Wi-Fi has no internet; `sudo nft list table inet bigeye` shows the rules.

## 2. Pair the cameras once

The 360 camera must be paired with the vendor app once (it needs the real cloud for that). Open a temporary, logged way
out for pairing: `sudo bash camera-machine/bigeye-pairing.sh on`, pair, then `sudo bash camera-machine/bigeye-pairing.sh off`.
To let only your phone out (the cameras stay locked in): `sudo bash camera-machine/bigeye-pairing.sh on <phone MAC>`.

**Check:** the cameras show up in `ip neigh show dev <interface>`.

## 3. Stand-in clouds (camera machine)

- 360 camera: set `BIGEYE_360_UUID` (the ID on the camera label) in `systemd/bigeye-fakecloud.service`, install and start it.
- Door/window cameras: start `bigeye-iwfcloud.service`, then let it learn the real cloud's answers once: write
  `<camera UID> <unix time until>` into `~/.config/sentinel-bigeye/iwfcloud-relay` for a few minutes during pairing mode.
  The answers are saved in `~/.config/sentinel-bigeye/iwfcloud-learned.json` and used from then on.

**Check:** `~/bigeye-fakecloud.log` shows the 360 signing in and `socket.io ping` lines; the 360 no longer drops off Wi-Fi
every few minutes.

## 4. Bridges (camera machine)

- 360: set `CAM360_MAC` and `CAMERA_IFACE` in `systemd/bigeye-bridge-cam360.service`, start it.
- door / window: see [bridges/iwfcam/README.md](../bridges/iwfcam/README.md).

**Check:** `curl http://127.0.0.1:8767/status` says `"state": "streaming"`.

## 5. Get the video to the main machine

If both roles are one box, skip this. Otherwise forward the bridge ports, for example:
`ssh -N -L 18767:127.0.0.1:8767 -L 18081:10.42.0.1:18080 cameras` (360 video + controls, the stand-in's caught uploads).

**Check:** `curl http://127.0.0.1:18767/status` works on the main machine.

## 6. The AI service (main machine)

```bash
uv venv -p 3.12 ~/.local/share/sentinel-bigeye/ai-venv
uv pip install -p ~/.local/share/sentinel-bigeye/ai-venv/bin/python "onnxruntime-gpu[cuda,cudnn]" numpy pillow insightface opencv-python-headless
uv pip uninstall -p ~/.local/share/sentinel-bigeye/ai-venv/bin/python onnxruntime   # insightface pulls the CPU build; keep only the GPU one
# Export YOLO11 once in a throw-away venv (AGPL-3.0; the weights stay on your machine):
uv venv -p 3.12 /tmp/export && uv pip install -p /tmp/export/bin/python --index-url https://download.pytorch.org/whl/cpu torch torchvision \
  && uv pip install -p /tmp/export/bin/python ultralytics onnx onnxslim
mkdir -p ~/.local/share/sentinel-bigeye/models && cd ~/.local/share/sentinel-bigeye/models \
  && /tmp/export/bin/python -c "from ultralytics import YOLO; YOLO('yolo11s.pt').export(format='onnx', imgsz=640, simplify=True)"
```

Start `systemd/bigeye-ai.service`. **Check:** `curl http://127.0.0.1:8771/status` says `"device": "GPU"` (or CPU).

## 7. The page (main machine)

1. `cp bigeye.example.toml ~/.config/sentinel-bigeye/bigeye.toml` and fill in your folders, cameras and (optional) ntfy.
2. Adjust the camera list in `systemd/bigeye-view.service`, install and start it.

**Check:** `http://localhost:8770` shows LIVE on each camera; the Call-home tab says "live from the camera machine".

## 8. Tests

`python3 -m pytest tests/` (no cameras needed).
