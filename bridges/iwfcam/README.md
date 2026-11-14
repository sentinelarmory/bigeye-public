# iWFCam bridge (door / window cameras): being rewritten

The bridge used during development was built on an existing open project that has **no licence**, so it cannot be
shared here. A new bridge is being written from scratch for this repo, from our own protocol notes
([../../docs/protocols.md](../../docs/protocols.md)), with:

- LAN-only session (no cloud rendezvous), H.264 (door) and MJPEG (W2 window) video
- the same HTTP control API the page already uses (`/ir`, `/pir`, `/control/picture`, `/control/light`,
  `/control/resolution`, `/control/keyframe`, `/control/time`, `/control/query`, `/control/sleepflags`, `/reboot`, `/status`, `/audio.wav`)
- key-frame replay, so a viewer that joins late gets a picture at once (the door camera sends a key frame only when its stream starts)
- gentle reconnects (these cameras fall off Wi-Fi when they get many new sessions in a short time)

Until it lands, the 360 camera is fully supported and the page works with any MJPEG or RTSP source.
