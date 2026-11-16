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
- door / window: build `bridges/doorbridge` (`go build`), fill in `systemd/bigeye-bridge-door.service` (camera address and
  its 12-character LAN token) and start it. **Check:** `curl http://127.0.0.1:8765/status` shows `"frame_seen": true`.
  After a factory reset the camera has a new LAN token; see [bridges/doorbridge/README.md](../bridges/doorbridge/README.md).

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
# Export the detector(s) once in a throw-away venv; the weights stay on your machine.
uv venv -p 3.12 /tmp/export && uv pip install -p /tmp/export/bin/python --index-url https://download.pytorch.org/whl/cpu torch torchvision \
  && uv pip install -p /tmp/export/bin/python rfdetr onnx onnxsim
mkdir -p ~/.local/share/sentinel-bigeye/models && cd ~/.local/share/sentinel-bigeye/models
# RF-DETR Small (Apache-2.0), the default detector:
/tmp/export/bin/python -c "from rfdetr import RFDETRSmall; RFDETRSmall().export(output_dir='.')"
# Optional fallback, YOLO11s (AGPL-3.0):
uv pip install -p /tmp/export/bin/python ultralytics onnxslim \
  && /tmp/export/bin/python -c "from ultralytics import YOLO; YOLO('yolo11s.pt').export(format='onnx', imgsz=640, simplify=True)"
```

**Older NVIDIA cards (GTX 10-series, Pascal):** the newest `onnxruntime-gpu` is built for CUDA 13, which no longer
supports them ("CUBLAS failure 8: architectural feature absent"). Install `"onnxruntime-gpu[cuda,cudnn]==1.22.0"`
(CUDA 12) and `nvidia-cudnn-cu12==9.5.1.17` (newer cuDNN 9.x fails on these cards with "CUDNN_FE failure 11");
a GTX 1060 then runs RF-DETR Small in about 56 ms per picture.

Face models (YuNet + SFace, about 40 MB, used by default):

```bash
cd ~/.local/share/sentinel-bigeye/models
curl -LO https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx
curl -LO https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx
```

Faces of other people are personal data: the stranger groups stay on this machine and are dropped after 30 days unseen;
check the rules where you live before you point a camera at places other people use.

The detectors are tried in the order of `BIGEYE_MODELS` (default `rfdetr-small.onnx,yolo11s.onnx`); a missing file
is skipped. Leave YOLO11 out for a setup with no AGPL parts in the detector.

Start `systemd/bigeye-ai.service`. **Check:** `curl http://127.0.0.1:8771/status` shows the model in use, its
`"device": "GPU"` (or CPU) and the order (`"chain"`).

## 7. The page (main machine)

1. `cp bigeye.example.toml ~/.config/sentinel-bigeye/bigeye.toml` and fill in your folders, cameras and (optional) ntfy.
2. Adjust the camera list in `systemd/bigeye-view.service`, install and start it.

**Check:** `http://localhost:8770` shows LIVE on each camera; the Call-home tab says "live from the camera machine".

## 8. Tests

`python3 -m pytest tests/` (no cameras needed).
