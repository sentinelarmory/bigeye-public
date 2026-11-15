# bigeye-doorbridge

A small, cloud-free bridge for one door or window camera on the local Wi-Fi.
It opens a LAN session with the camera over UDP, receives its video and audio,
and serves them as:

- **RTSP** (H.264 video + G.711 µ-law audio) for the door model,
- **MJPEG over HTTP** plus a JPEG snapshot for the W2 battery (window) model,
- **streaming WAV** audio over HTTP,
- an **HTTP control API** (night vision, light, picture, clock, sleep settings, ...)
  and JSON status (battery, frame age, last camera replies).

When the session dies it reconnects with back-off. One process serves one camera;
for two cameras run two processes with different ports.

This program was written from the functional and protocol specification
`SPEC.md` (clean-room rewrite). See `PROVENANCE.md`.

## Build

Requires Go 1.23. Only the standard library and
`github.com/bluenviron/gortsplib/v4` (with its `mediacommon` dependency) are used.

```sh
go build -trimpath -ldflags="-s -w" -o bigeye-doorbridge .

# static Linux x86-64 binary
CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o bigeye-doorbridge .
```

Offline build with an existing module cache:
`GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off go build ...`

Tests (no camera or network needed; one test uses the loopback interface):

```sh
go vet ./... && go test ./...
```

## Run

```sh
./bigeye-doorbridge -ip 192.168.1.60 -password-hash ABCDEFGHIJKL
```

Then open `http://<host>:8765/` for the endpoint list, or
`rtsp://<host>:8554/cam` in a player.

### Flags

| Flag | Default | Meaning |
|---|---|---|
| `-ip` | (required) | camera IPv4 address |
| `-password-hash` | (required) | the camera's 12-character LAN token (changes after a factory reset) |
| `-uid` | `""` | camera UID; only logged |
| `-cloud` | `""` | accepted for compatibility; must be empty, no cloud is ever contacted |
| `-stream-port` | `0` | camera media UDP port; 0 = discover it on every (re)connect |
| `-local-port` | `36891` | local UDP port for the session; a random port is used if it is busy |
| `-rtsp-port` | `8554` | RTSP listen port (all interfaces); 0 disables RTSP |
| `-http` | `:8765` | HTTP listen address; empty disables HTTP |
| `-media-timeout` | `15` | seconds without a complete video frame before reconnecting (at least 6 s are allowed for the first frame) |
| `-stream-mode` | `3` | value sent in the start-stream request (3 = live view) |
| `-advertise-ip` | `""` | IPv4 to put into the hello instead of the auto-detected local address |
| `-dump` | `""` | append the raw H.264 Annex-B stream to this file (debugging) |

### Endpoints

Media and status:

| Path | Description |
|---|---|
| `/` | small HTML page linking the endpoints |
| `/cam.mjpg` | `multipart/x-mixed-replace` stream of JPEG frames (W2 model) |
| `/snapshot.jpg` | newest JPEG frame (404 until one has arrived) |
| `/audio.wav` | endless 8 kHz mono 16-bit WAV; silence is inserted when audio pauses |
| `/status` | `battery_pct`, `battery_mv`, `charging`, `status_seen`, `status_age_s`, `frame_seen`, `frame_age_s` |
| `/control/state` | `last_by_cmd`, `media`, `sleep_config`, `sleep_raw`, `night_vision` |
| RTSP `/cam` (any path) | H.264 (PT 96) + G.711 µ-law (PT 0); TCP interleaved transport |

Controls (answer `{"queued": "<label>"}`; 400 on bad parameters, 503 when the
8-entry queue is full):

| Path | Method | Effect |
|---|---|---|
| `/ir?on=1\|0` | GET/POST | night vision on/off |
| `/pir?on=1\|0` | GET/POST | motion detection on/off |
| `/control/light?on=1\|0` | GET/POST | light on/off |
| `/control/picture?brightness=N` / `contrast=N` / `turn=flip\|mirror` | GET/POST | picture settings (0..100; flip/mirror toggle) |
| `/control/resolution?size=WxH` | GET/POST | resolution, 160x120 .. 4096x3072 |
| `/control/audio?on=1\|0` | GET/POST | camera audio start/stop |
| `/control/query?what=sleep\|ir\|alarm\|media` | GET/POST | ask the camera; the answer shows up in `/control/state` |
| `/control/keyframe` | POST | request a key frame |
| `/control/time` | POST | set the camera clock to this machine's local time |
| `/reboot` | POST | reboot the camera |
| `/control/sleepflags?set=BITS&clear=BITS` | POST | change sleep flags (409 until a sleep config was read) |
| `/control/sleepraw?hex=<176 hex chars>` | POST | write a raw 88-byte sleep config |
| `/control/probe?cmd=N&sub=0\|1` | POST | send a harmless read for discovery |

`on` accepts `1/0`, `true/false`, `on/off`. Numbers in `set`, `clear` and `cmd`
may be decimal or `0x` hex.

## Behaviour notes

- Reconnect back-off starts at 2 s, doubles after every session shorter than
  60 s, is capped at 60 s, and resets after a session longer than 60 s. These
  cameras drop off Wi-Fi when they see many new sessions quickly, so the bridge
  never retries faster than this.
- A new RTSP viewer immediately receives the last stored key frame (the door
  camera only sends one when its stream starts). The library sends it to all
  active viewers; existing viewers just get one extra key frame.
- On SIGINT/SIGTERM the bridge stops the stream, closes the session politely and exits.

## License

AGPL-3.0 (repository license).
