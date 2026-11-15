# Provenance

- Date: 2026-11-16
- Author: Claude (Anthropic model), working as the "clean room" side of a
  clean-room rewrite, on a private machine.

## Statement

All code in this folder was written only from the written specification
`docs/doorbridge-spec.md (in this repo)`. No other bridge
implementation was consulted: no existing bridge source code, binaries,
decompilations or packet-handling code for these cameras was opened, searched
or listed, and no web or GitHub search for similar projects was made. No camera,
other machine or network service was contacted while writing it; builds were
offline and tests run locally (one test uses the loopback interface only).

## Files read

1. `docs/doorbridge-spec.md (in this repo)` (the only source of
   protocol knowledge).
2. Library sources in the local Go module cache, to learn their APIs:
   - `github.com/bluenviron/gortsplib/v4@v4.9.0`: `go.mod`,
     `examples/server/main.go`, `server.go` (config fields, `Start`),
     `server_handler.go` (handler interfaces and contexts),
     `server_session.go` (session states and the PLAY sequence),
     `server_stream.go` (`NewServerStream`, `WritePacketRTP*`),
     `server_conn.go` (request/response hook order, `NetConn`),
     `client.go` (client methods used by the loopback test),
     `transport.go`, `pkg/format/h264.go`, `pkg/format/g711.go`,
     `pkg/format/rtph264/encoder.go`, `pkg/format/rtplpcm/encoder.go`,
     `pkg/description/media.go`, `pkg/description/session.go`, and a generic
     sample H.264 SPS from `client_record_test.go` (used as test data).
   - `github.com/bluenviron/mediacommon@v1.10.0`: directory listing of
     `pkg/codecs/h264` and the exported function names in `annexb.go`
     (not used in the end; the bridge has its own Annex-B splitter).
3. Public algorithms implemented from general knowledge: the IMA ADPCM step and
   index tables and decoding procedure, G.711 µ-law encoding, the WAV header
   layout.

## Toolchain

Go 1.23.4, `gortsplib/v4 v4.9.0`, `mediacommon v1.10.0`, offline
(`GOFLAGS=-mod=mod GOPROXY=off GOSUMDB=off`).
