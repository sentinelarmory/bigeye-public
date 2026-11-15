# Camera protocols (as decoded and verified)

## Cam360 — ZAS-Q01, CS2 PPCS over UDP (LAN only)

Session: `F1 30` LanSearch to UDP 32108 → camera answers `PUNCH_PKT 0x41` (20-byte body)
from a rotating port → echo it as `0x41`, then `0x42 P2P_RDY` → DRW `0xD0` packets
`F1 D0 <u16 BE len> D1 <channel> <u16 BE index> <body>`, every DRW acked with `0xD1`;
keep-alive `0xE0`/`0xE1` every 10 s. Command body: `<u32 LE ioType><u32 LE len><payload>`.

| ioType | Payload | Meaning (verified 2026-11-14 unless noted) |
|---|---|---|
| 0x8002 (32770) | 60 bytes, password at offset 8 | login (reply 0x8003); the password changes at every boot; the stand-in cloud learns it |
| 0x8024 | – | feature JSON (reply 0x8025): AutoTracking, SupportPTZ, DayNight, Resolutions `0FHD+1HD`, Cap-AI body/pet/car … |
| 0x01FF (511) | `<u32 2><u32 0>` | start video (stream 2 only; 0 = invalid, 1/3 = no video) |
| 0x0320 | `<u32 0><u32 q>` | quality: 1 = 1920x1080, every other value (incl. app "HD" = 5) = 640x360 |
| 0x1001 | `<u32 dir><u32 0>` | relative PTZ nudge: 1 up, 2 down, 3 left, 6 right, 0 stop |
| 0x0408 | `<f32 x><f32 y><u32 4><u32 0><u32 n>` | **absolute position**, x = pan, y = tilt, both 0..1 (floats honoured, n irrelevant); beyond 0..1 the motor does not go further (negative pan is erratic) |
| **0x8018** | `<u32 0><u32 mode><u32 0>` | **day/night**: 0 auto, 2 night/IR (picture saturation → 0), 1 day |
| **0x0414** | `<u32 on><u32 0>` | **camera on/off**: 0 stops all video (commands still answered). Bridge sends 1 at every connect |
| **0x8022** | `<u32 0><u32 on>` | **auto-tracking** (follows people, burns a green frame into the video) |
| 0x0310 | `<u32 0><u32 m><u32 1>` | sent by the app around night mode; no visible effect |
| 0x0324 | 446 bytes | motion detection area/sensitivity (not used) |

Earlier labels taken "by order of button presses" were wrong for 0x0310 (not night),
0x0414 (not tracking) and 0x8022 (not "device switch"); the public notes in
[jphein/eyeplus-icam365-camera](https://github.com/jphein/eyeplus-icam365-camera) (SET_DAYNIGHT 32792, MotionTrack 32800/32802)
gave the right ids.

Video: DRW channel 2, byte stream of frames with a 16-byte header
`<u16 codec (80 = H.265)><u16 flags (1 = key)><u32 ?><u32 length><u32 ms>` + Annex-B.
Audio: channel 1, A-law.

## Door / window — JT-B9 and W2 (`2TEG` control packets, LAN-only `1TEG` hello)

Control packet: `"2TEG" <u16 1> <u16 len> <u32 session> <u32 cmd> <u32 subcmd> <u32 datalen> <u32 0> <payload>`.

| cmd / sub | Meaning |
|---|---|
| 0x13 sub 3 / 4 | night vision on / off; sub 1 read → payload byte 8 = 1 when on |
| 0x13 sub 5 + 12 bytes `ff 00 XX 00 ff …` | work light (XX 1 = on) |
| 0x06 / 0x07, value in subcmd | brightness / contrast 0..100 |
| 0x08 sub 1 / 2 | flip / mirror toggle |
| 0x04, subcmd = w<<16 \| h | stream size: **door has two real modes** — any small size → 640x360, 1280x720/1920x1080/"4K"/"2K" → 1920x1080 (cmd 15 media info always says 1920x1080) |
| 0x0c sub 1 | key frame request (door ignores it) |
| 0x15 sub 2 + 32 bytes | set clock |
| 0x22 sub 0 / 1 + 88 bytes | sleep config read/write (W2 window only; door does not answer) |
| 0x0d sub 10 + 12 bytes | PIR alarm on/off (window) |
| 0x01 sub 1 / 0 | audio start/stop |
| 0x50 | reboot (works while the camera still answers) |

The door camera sends an H.264 IDR only when its stream starts; the bridge keeps the
last key-frame access unit (SPS+PPS+slices) and replays it when a new RTSP viewer
plays (see lessons).
