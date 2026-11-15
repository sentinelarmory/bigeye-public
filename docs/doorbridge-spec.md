# Door / window camera bridge — functional and protocol specification

This document is the only input for writing the new bridge. It describes **what** the bridge must do and the
**facts** of the cameras' LAN protocol (packet layouts, numbers, timings), as observed in our own packet captures
and tests in 2026-11. It deliberately contains no source code. Write the program from this text, in your own
structure and words.

All multi-byte integers are **little-endian** unless marked BE (big-endian). Offsets are byte offsets from the
start of the UDP payload. "u16"/"u32" = unsigned 16/32-bit.

## 1. What the program is

A single Go program (one binary) that runs on a Linux machine on the same Wi-Fi as **one** camera and:

1. opens a cloud-free session with the camera over UDP,
2. receives its video (H.264 from the door model, JPEG frames from the W2 battery model) and audio,
3. serves the video as **RTSP** (H.264 + G.711 µ-law audio) and/or as **MJPEG over HTTP**, plus audio as a
   streaming WAV over HTTP,
4. offers an HTTP API to send camera controls (night vision, light, picture, clock, sleep settings, …) and to read
   status (battery, last frame time, last replies),
5. keeps running forever: when the session dies it reconnects, with back-off.

One process per camera. Two cameras = two processes with different ports.

### Command-line flags (all required to exist; defaults in brackets)

| Flag | Meaning |
|---|---|
| `-ip` | camera IPv4 address (required) |
| `-password-hash` | the camera's 12-character LAN token (ASCII); required |
| `-uid` | camera UID string; accepted for compatibility, only logged |
| `-cloud` | accepted for compatibility; must be `""`; the bridge never contacts any cloud |
| `-stream-port` [0] | camera's media UDP port; 0 = discover it on every (re)connect (section 3) |
| `-local-port` [36891] | local UDP port for the session; if busy, fall back to a random port |
| `-rtsp-port` [8554] | RTSP listen port (all interfaces); `0` disables RTSP |
| `-http` [`:8765`] | HTTP listen address (MJPEG, audio, control API); empty disables HTTP |
| `-media-timeout` [15] | seconds without any complete video frame before the session is considered dead |
| `-stream-mode` [3] | value sent in the start-stream request (3 = live view) |
| `-advertise-ip` [""] | IPv4 to put into the hello instead of the auto-detected local address |

The program must log in plain English, one line per event, with timestamps (standard Go `log` is fine).

## 2. Packet families

Every packet starts with a 4-byte ASCII magic:

- `"1TEG"`: discovery and the LAN hello (session setup)
- `"2TEG"`: everything inside a session (handshake replies, controls, media, acknowledgements)

### 2TEG common header (8 bytes)

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `"2TEG"` |
| 4 | u16 | packet kind |
| 6 | u16 | length of everything **after** these 8 bytes |

Kinds seen: **0** handshake, **1** control (request or camera-initiated message), **2** control reply,
**3** media data, **4** acknowledgement / retransmission request (sent by us).

## 3. Discovering the camera's media port

The camera answers discovery on **UDP port 10104**.

- Bind a local UDP socket to port 10104 if possible (otherwise any port) and send this 20-byte request to
  `<camera>:10104`: magic `"1TEG"`, u16 **11**, u16 **12**, u16 **1**, then 10 zero bytes.
- The camera answers with a `"1TEG"` packet of at least 36 bytes. The media port is the **BE u16 at offset 34**.
  If that value is 0, use the source port of the answer if it is not 10104.
- Try 5 times, waiting 1.2 s for an answer each time. Ignore answers from other addresses.
- Observed answer: 76 bytes.

## 4. LAN hello (cloud-free session start)

Open the session socket (the `-local-port`). Send to `<camera>:<media port>` a 48-byte packet:

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | `"1TEG"` |
| 4 | u16 | 13 |
| 6 | u16 | 40 |
| 8..25 | 18 | zero |
| 26 | BE u16 | our local UDP port |
| 28 | 4 | our local IPv4 address (4 bytes, network order) |
| 32 | 1 | `0x09` |
| 33 | 12 | the 12-character LAN token (ASCII) |
| 45..47 | 3 | zero |

- "Our local IPv4" is the address of the interface that routes to the camera (or `-advertise-ip`).
- Send it up to 5 times, 1 s apart, within a 6 s window; read with short timeouts in between.
- The camera accepts the token if it answers with **either** a `"1TEG"` packet of kind **14** (≥ 28 bytes; the
  session id is the u32 at offset **24**) **or** directly with a `"2TEG"` packet (the session id is the u32 at
  offset **8**). A session id of 0 is not an answer. Only accept packets from the camera's address.
- The camera chooses the session id: 1 for the first viewer, 2 for a second simultaneous viewer, and so on.
  (So two bridges can be connected to the same camera at the same time.)
- A wrong token: the camera answers discovery but stays silent to the hello. Log this clearly, e.g.
  "camera did not answer the hello: wrong LAN token? (it changes after a factory reset)".

## 5. Session bootstrap (right after the hello)

All packets in this section are 2TEG, sent to the camera's media port, with the session id from section 4.

**Control packet layout** (kind 1), used for every command:

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `"2TEG"` |
| 4 | u16 | 1 |
| 6 | u16 | 20 + payload length |
| 8 | u32 | session id |
| 12 | u32 | cmd |
| 16 | u32 | subcmd |
| 20 | u32 | payload length |
| 24 | u32 | 0 |
| 28 | … | payload |

Bootstrap sequence, 20 ms apart:

1. kind-1 packet, cmd 0, subcmd 1, payload length 8, payload 8 zero bytes (an "open" with an empty 8-byte body)
2. cmd 13 sub 1 (no payload)
3. cmd 14 sub 2
4. cmd 15 sub 0
5. cmd 15 sub 2
6. cmd 21 sub 0
7. cmd 19 sub 1
8. cmd 26 sub 0
9. **start stream**: cmd 0, subcmd 1, payload length 8, payload = u32 0, u32 `-stream-mode` (3)

The camera begins sending media (kind 3) within a few seconds. It may take up to ~6 s for the first complete frame.

**Keepalive**: every 2 s send a kind-1 packet, cmd **2**, subcmd 0, payload length 8, payload = u32 0, u16 **15**
(the viewer's play rate, frames per second), u16 0.

**Ending a session** (on shutdown or before reconnecting): send "stop stream" (same as start stream with mode 0)
twice, 15 ms apart, then cmd **3** sub 1 twice, 10 ms apart.

## 6. Media packets (kind 3) and reassembly

| Offset | Size | Field |
|---|---|---|
| 6 | u16 | data length L (bytes after offset 8) |
| 14 | u16 | media type (copy it into retransmission requests) |
| 16 | u16 | **frame number** |
| 18 | u16 | **packet number within the frame**, from 0 |
| 20 | u32 | payload length P |

The payload is the **last P bytes** of the packet: payload offset = 8 + L − P. Reject packets where P ≤ 0, the
offset is below 24, or offset + P exceeds the packet.

- **Packet 0 of a video frame** has a longer header (payload offset ≥ 40). In it, u32 at offset **28** is the
  number of packets in the frame, u16 at **32** the width and u16 at **34** the height. Trust the count only if it
  is 1..99 and the size is plausible (width 160–4096, height 120–2160).
- **Audio**: a packet with packet number 0, payload offset exactly **28** and payload length **516** is one audio
  block (see section 8). It is not part of a video frame; acknowledge its frame number right away.
- A frame is complete when packets 0..count−1 are all present. Its bytes are the payloads concatenated in packet order.
- **What a complete frame contains**:
  - W2 window model: a JPEG (starts with `FF D8`). It is zero-padded after the end marker `FF D9`: cut at the
    **last** `FF D9`.
  - Door model: H.264 Annex-B (a start code `00 00 00 01` or `00 00 01` within the first 12 bytes; skip any bytes
    before it). Split into NAL units.
- Keep at most a few seconds of incomplete frames. A frame that is still incomplete after 16 retransmission rounds
  or 4 s is abandoned (and acknowledged, so the camera stops resending it).
- The camera may resend packets of a frame we already finished: acknowledge such a frame again and drop the data.
  Remember the last ~180 finished frame numbers for this.

## 7. Acknowledgements and retransmission requests (kind 4, sent by us)

Without these the camera throttles or stalls (large frames never complete).

| Offset | Size | Field |
|---|---|---|
| 0 | 4 | `"2TEG"` |
| 4 | u16 | 4 |
| 6 | u16 | 8 + 8 × N |
| 8 | u32 | session id |
| 12 | u8 | our measured frame rate (1..255, frames completed in the last second; start at 15) |
| 13 | 2 | zero |
| 15 | u8 | N = number of items |
| 16 | 8 × N | items |

Each item is four u16:

- **ACK of a complete frame**: `0x0201, 0, frame number, 0xFFFF`
- **Request a missing packet**: `0, media type, frame number, packet number`

Rules (as the vendor app does it):
- Send a kind-4 packet whenever a frame completes, after every 15 media packets received, and at least every 40 ms
  while anything is pending. Never send an empty one.
- At most 123 items per packet.
- Missing-packet requests for a frame start when the frame is ≥ 240 ms old, then at most every 240 ms per frame,
  at most 64 packet numbers per frame per round, at most 16 rounds. If the frame's packet count is not known yet,
  ask up to (highest packet number seen + 3).

## 8. Audio

An audio block (516 bytes) is **IMA ADPCM**, mono, **8000 Hz**:

- bytes 0–1: initial predictor (signed 16-bit), byte 2: step index (clamp to 0..88), byte 3: unused
- then 512 bytes, two 4-bit codes per byte, **low nibble first** → 1 + 1024 samples per block
- decode with the standard IMA ADPCM step table and index table (public algorithm).

Outputs:
- **RTSP**: convert each sample to G.711 **µ-law** (standard algorithm) and send as the audio track
  (payload type 0, 8000 Hz, mono); the RTP timestamp advances by the number of samples.
- **HTTP** `/audio.wav`: a streaming WAV: 44-byte header (PCM, mono, 8000 Hz, 16-bit, RIFF and data sizes
  `0xFFFFFFFF`), then 16-bit little-endian samples as they arrive. If nothing arrives for 300 ms, write 200 ms of
  silence so players do not stall.

## 9. Video outputs

### RTSP (door, H.264)
- One stream, path `/cam` (accept any path), H.264 video (payload type 96, packetization mode 1) and G.711 µ-law
  audio. Use the library `github.com/bluenviron/gortsplib/v4` (available in the local Go module cache; MIT).
- Track SPS (NAL type 7) and PPS (type 8) from the stream and set them on the H.264 format as they appear.
- RTP timestamps from wall-clock time since the session started.
- **Key-frame replay (essential)**: the door camera sends an IDR (NAL type 5) only when its stream starts. Keep the
  last key-frame access unit (SPS + PPS + the slices of that frame, NAL types 5 and 1). When a new RTSP client sends
  PLAY, send that stored access unit once, right away (before the next frame), so the new viewer gets a picture
  immediately. Log "replayed the last key frame for a new viewer".
- Optional `-dump <file>`: append the raw Annex-B stream (debugging).

### MJPEG over HTTP (W2, JPEG frames)
- `/cam.mjpg`: `multipart/x-mixed-replace` stream of the latest JPEG frames (start with the newest one at once).
  Slow clients must not block others: drop frames for a client that is behind.
- `/snapshot.jpg`: the newest JPEG (404 when none yet).
- For H.264 cameras these simply have no frames (that is fine).

## 10. Camera controls (HTTP API → kind-1 control packets)

Controls go through a small queue (8 entries) into the session; if the queue is full answer **503**. Answer
`{"queued": "<label>"}` (JSON) when queued. Some commands are sent several times (`repeat`, with a gap), because
the cameras sometimes miss one.

| HTTP | Method | Packet(s) |
|---|---|---|
| `/ir?on=1` / `?on=0` | GET/POST | cmd **0x13**, sub **3** (on) / **4** (off); send 3×, 20 ms apart |
| `/pir?on=1|0` | GET/POST | cmd **0x0d**, sub **10**, 12-byte payload, byte 0 = 1 (on) / 0 (off) |
| `/control/light?on=1|0` | GET/POST | cmd **0x13**, sub **5**, 12-byte payload `FF 00 XX 00 FF 00 00 00 00 00 00 00`, XX = 1 on / 0 off; 2×, 20 ms |
| `/control/picture?brightness=N` | GET/POST | cmd **6**, subcmd = N (0..100) |
| `/control/picture?contrast=N` | GET/POST | cmd **7**, subcmd = N (0..100) |
| `/control/picture?turn=flip` / `turn=mirror` | GET/POST | cmd **8**, sub **1** (flip) / **2** (mirror) — each is a toggle |
| `/control/resolution?size=WxH` | GET/POST | cmd **4**, subcmd = (W << 16) \| H; accept 160x120 .. 4096x3072 |
| `/control/keyframe` | POST only | cmd **0x0c**, sub **1** |
| `/control/time` | POST only | cmd **0x15** (21), sub **2**, 32-byte payload: u32 year, month, day, hour, minute, second (local time), u32 unix time, u32 0 |
| `/control/audio?on=1|0` | GET/POST | cmd **1**, sub **1** (start) / **0** (stop) |
| `/reboot` | POST only | cmd **0x50**; 3×, 200 ms apart |
| `/control/query?what=sleep|ir|alarm|media` | GET/POST | read: sleep = cmd 0x22 sub 0; ir = cmd 0x13 sub 1; alarm = cmd 0x0d sub 1; media = cmd 0x0f sub 0 |
| `/control/sleepflags?set=BITS&clear=BITS` | POST only | read-modify-write of the last sleep config read (409 if none yet): flags u32 at config offset 8, new = (old \| set) & ~clear; send cmd 0x22 sub 1 with the full 88-byte config |
| `/control/sleepraw?hex=<176 hex chars>` | POST only | cmd 0x22 sub 1 with these 88 bytes |
| `/control/probe?cmd=N&sub=0|1` | POST only | a read for discovery: refuse cmd 0, 3, 4, 6, 7, 8 and 0x50, refuse sub > 1 and cmd > 0xff |

Parameter errors → **400** with a one-line explanation. `on` accepts 1/0, true/false, on/off.

## 11. Messages from the camera (kind 1 and kind 2) and status

Record, per cmd, the latest kind-1/kind-2 packet from the camera as a text line
`"type<kind> sub<subcmd> <hex of bytes 12..end>"` (for `/control/state`).

Known messages:
- **Status report** (kind 1, exactly 48 bytes, cmd 13, subcmd 9; W2 sends it periodically): u32 at offset **40**:
  low byte = battery percent, bit 8 (0x100) = on charger; u32 at offset **44** = battery millivolts.
- **Media info** reply (cmd 0x0f, subcmd 0, payload from offset 28, ≥ 20 bytes): payload u32 [0] = 0 H.264 /
  1 MJPEG; payload u32 at payload offset 12 = width (low 16 bits) | height (high 16 bits); payload u32 at payload
  offset 16 = profile/quality index.
- **Sleep config** reply (cmd 0x22, subcmd 0, 88-byte payload at offset 28): byte 0 version, 1 last-wakeup flag,
  2 battery %, 3 PIR level, u16 at 4 battery mV, u16 at 6 idle wake time, u32 at 8 flags (bit 0 sleep enabled,
  bit 1 wake from LAN, bit 2 sleep when idle, bit 3 no sleep on charger), u32 at 12 wake-up time, u32 at 16 and 20
  wake-up functions. Keep the raw 88 bytes (for sleepflags).
- **Night vision read** reply (cmd 0x13 sub 1): payload byte 8 = 1 when night vision is on (expose it in state).
- cmd **3** from the camera: it closed the session → end this session and reconnect.
- cmd **0x23** from the camera: it announces it is going to sleep (W2) → log it.

### `/status` (JSON)
`battery_pct`, `battery_mv`, `charging`, `status_seen` (a status report has arrived), `status_age_s` (−1 if never),
`frame_seen`, `frame_age_s` (age of the newest complete video frame, JPEG **or** H.264; −1 if never).

### `/control/state` (JSON)
`last_by_cmd` (map of cmd as decimal string → line), `media` (codec, width, height, profile, received_unix) or null,
`sleep_config` (the parsed fields above + received_unix) or null, `sleep_raw` (hex), `night_vision` (true/false/null).

### `/` 
A tiny HTML page linking the endpoints (no external resources).

## 12. Session life cycle and robustness

- A session ends when: no complete frame for `-media-timeout` seconds once frames have started (allow at least 6 s
  for the first frame), the camera sends cmd 3, or the socket fails. On end, send the "ending a session" packets.
- Reconnect loop with **back-off**: start at ~1–2 s, double after every session that lasted less than 60 s, cap at
  60 s; reset to the base delay after a session that lasted longer than 60 s. Rediscover the port each time when
  `-stream-port` is 0.
- **Why**: these cameras fall off Wi-Fi (and stop answering discovery until power-cycled) when they get many new
  sessions in a short time. Never retry faster than this.
- Every 10 s log one status line (seconds since the last good frame, frames pending).
- On SIGINT/SIGTERM: end the session politely and exit.
- The RTSP server, the HTTP server and the outputs live for the whole process; only the camera session restarts.

## 13. Quality bar

- Go, `go 1.23` module, only standard library + gortsplib v4 (+ its mediacommon dependency). Build offline:
  `go build -trimpath -ldflags="-s -w"` must work with the module cache that is on this machine.
- Small, readable files by responsibility (e.g. session, media, audio, http/api, rtsp, main). English comments.
- Unit tests for the parts that need no camera: packet builders (exact bytes), media header parsing, frame
  reassembly (incl. JPEG trimming and Annex-B start), ACK/NAK packet building, IMA ADPCM decode against a known
  vector, µ-law conversion, status/media/sleep parsing, back-off timing.
- A `README.md`: what it is, flags, endpoints, how to build and run, and that it is written from this spec.
- License header not needed per file; the repo is AGPL-3.0.
