# Architecture

## Machines

- **Camera machine** — any Linux box with a spare Wi-Fi card. Hosts the isolated camera Wi-Fi
  (10.42.0.1/24), runs the bridges, the stand-in clouds and the firewall.
- **Main machine** — runs the page, recording, add-ons and the AI service (Linux or WSL; an NVIDIA GPU is optional).
- The two can be one box. When they are two, the video ports are forwarded over SSH.

## Data paths

| Camera | Camera → the camera machine | the camera machine → main machine | Page reader |
|---|---|---|---|
| Cam360 | CS2 PPCS (cam360-bridge.py) | raw H.265 `127.0.0.1:18767/cam360.h265` | ffmpeg (`-f hevc`, wall-clock timestamps) → JPEG |
| Door | LAN-only bridge (iwfcam-lan) | RTSP `127.0.0.1:18554/cam` | ffmpeg → JPEG (full-range conversion) |
| Window | LAN-only bridge, MJPEG | `127.0.0.1:18766/cam.mjpg` | pass-through (camera's own JPEGs) |

Controls: `127.0.0.1:18765` (door), `:18766` (window), `:18767` (360), proxied by the page
under `/ctl/<cam>/…` with an allow-list.

## Page server (…-view.py)

- newest JPEG per camera in memory + 4 s ring buffer (pre-roll)
- `/frame/<cam>?after=` long-poll, `/state`, `/snap/<cam>`, `/rec/<cam>`, `/settings/<cam>`,
  `/ptz/<cam>/…`, `/reconnect/…`, `/home`, `/autofix`, `/faces…`, `/pets`, `/telemetry…`
- recorder: frames → `ffmpeg -f mjpeg -framerate 15 … libx264 -crf 23` MP4
- add-ons loaded from `addons/*.py` with a small context (cameras, settings, status,
  trigger, PTZ goto/state/preset, register action, save)
- settings: `~/.config/sentinel-bigeye/settings.json`

## AI service (bigeye-ai.py, :8771)

- a list of ONNX detectors tried in order (`BIGEYE_MODELS`, default RF-DETR Small, Apache-2.0, then YOLO11s,
  AGPL-3.0, as an optional fallback), onnxruntime-gpu CUDA; classes person/cat/dog/bird/car/tv/laptop
- failover: a model that errors, loses the GPU or is slower than 200 ms (median of 20) is swapped at once;
  the first is tried again every 10 minutes; `/status` lists the order and the last switches
- `?faces=1`: InsightFace buffalo_l (det 320 + ArcFace) on the upper 65 % of each person
  box; cosine ≥ 0.40 against `faces.json`
- `/enroll?name=` needs exactly one person with a clear face

## Add-ons

| Add-on | Does |
|---|---|
| motion | 160x90 grey diff vs slow background, 2 hits, ignores lighting jumps (>55 %), paused while the camera turns |
| ai | detection 2×/s per camera, ignore zones, sticky names (8 s, IoU > 0.3), pet names, skip known faces, triggers per "Record when" |
| follow | turns the 360 toward the tracked person (deadband 0.13, damping 0.7), optional return to a favourite, "Learn my camera" phase-correlation calibration |
