package main

// RTSP output: H.264 video + G.711 mu-law audio with key-frame replay for
// new viewers (SPEC.md section 9, "RTSP").

import (
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/bluenviron/gortsplib/v4/pkg/format/rtph264"
	"github.com/bluenviron/gortsplib/v4/pkg/format/rtplpcm"
)

// keyFrameStore remembers parameter sets and the last key-frame access unit.
// It is pure bookkeeping so it can be tested without a server.
type keyFrameStore struct {
	sps, pps []byte
	keyAU    [][]byte
}

// observe inspects one access unit. It returns true when SPS or PPS changed.
func (k *keyFrameStore) observe(au [][]byte) bool {
	changed := false
	hasIDR := false
	for _, n := range au {
		switch naluType(n) {
		case naluSPS:
			if string(n) != string(k.sps) {
				k.sps = append([]byte(nil), n...)
				changed = true
			}
		case naluPPS:
			if string(n) != string(k.pps) {
				k.pps = append([]byte(nil), n...)
				changed = true
			}
		case naluIDR:
			hasIDR = true
		}
	}
	if hasIDR {
		stored := make([][]byte, 0, len(au)+2)
		if k.sps != nil {
			stored = append(stored, k.sps)
		}
		if k.pps != nil {
			stored = append(stored, k.pps)
		}
		for _, n := range au {
			if t := naluType(n); t == naluIDR || t == naluSlice {
				stored = append(stored, append([]byte(nil), n...))
			}
		}
		k.keyAU = stored
	}
	return changed
}

// rtspOutput owns the RTSP server and the single stream it serves.
type rtspOutput struct {
	server *gortsplib.Server
	stream *gortsplib.ServerStream
	video  *description.Media
	audio  *description.Media
	h264   *format.H264

	mu        sync.Mutex // guards everything below and orders writes
	vEnc      *rtph264.Encoder
	aEnc      *rtplpcm.Encoder
	start     time.Time
	keys      keyFrameStore
	audioTS   uint32
	audioNext time.Time // when the next audio block is expected
}

// newRTSPOutput starts an RTSP server on all interfaces.
func newRTSPOutput(port int) (*rtspOutput, error) {
	o := &rtspOutput{start: time.Now()}
	o.h264 = &format.H264{PayloadTyp: 96, PacketizationMode: 1}
	g711 := &format.G711{PayloadTyp: 0, MULaw: true, SampleRate: audioSampleRate, ChannelCount: 1}
	o.video = &description.Media{Type: description.MediaTypeVideo, Formats: []format.Format{o.h264}}
	o.audio = &description.Media{Type: description.MediaTypeAudio, Formats: []format.Format{g711}}

	var err error
	if o.vEnc, err = o.h264.CreateEncoder(); err != nil {
		return nil, err
	}
	if o.aEnc, err = g711.CreateEncoder(); err != nil {
		return nil, err
	}

	o.server = &gortsplib.Server{
		Handler:     &rtspHandler{out: o},
		RTSPAddress: fmt.Sprintf(":%d", port),
	}
	if err := o.server.Start(); err != nil {
		return nil, fmt.Errorf("cannot start the RTSP server: %w", err)
	}
	o.stream = gortsplib.NewServerStream(o.server, &description.Session{
		Title:  "bigeye-doorbridge",
		Medias: []*description.Media{o.video, o.audio},
	})
	return o, nil
}

func (o *rtspOutput) close() {
	o.stream.Close()
	o.server.Close()
}

func (o *rtspOutput) videoTimestamp(now time.Time) uint32 {
	return uint32(now.Sub(o.start).Seconds() * 90000)
}

// writeAU encodes and sends one access unit. o.mu must be held.
func (o *rtspOutput) writeAU(au [][]byte, now time.Time) {
	pkts, err := o.vEnc.Encode(au)
	if err != nil {
		log.Printf("rtsp: cannot encode video: %v", err)
		return
	}
	ts := o.videoTimestamp(now)
	for _, p := range pkts {
		p.Timestamp += ts
		_ = o.stream.WritePacketRTPWithNTP(o.video, p, now)
	}
}

// writeVideo publishes one complete H.264 frame (Annex-B).
func (o *rtspOutput) writeVideo(annexB []byte, now time.Time) {
	au := splitAnnexB(annexB)
	if len(au) == 0 {
		return
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.keys.observe(au) {
		o.h264.SafeSetParams(o.keys.sps, o.keys.pps)
		log.Printf("rtsp: H.264 parameter sets updated (SPS %d bytes, PPS %d bytes)", len(o.keys.sps), len(o.keys.pps))
	}
	o.writeAU(au, now)
}

// writeAudio publishes decoded samples as mu-law.
func (o *rtspOutput) writeAudio(samples []int16, now time.Time) {
	o.mu.Lock()
	defer o.mu.Unlock()
	// Keep the audio clock tied to wall time: resynchronise after a gap
	// or when it drifts too far ahead.
	if o.audioNext.IsZero() || now.Sub(o.audioNext) > 500*time.Millisecond || o.audioNext.Sub(now) > time.Second {
		o.audioTS = uint32(now.Sub(o.start).Seconds() * audioSampleRate)
		o.audioNext = now
	}
	pkts, err := o.aEnc.Encode(samplesToMuLaw(samples))
	if err != nil {
		log.Printf("rtsp: cannot encode audio: %v", err)
		return
	}
	for _, p := range pkts {
		p.Timestamp += o.audioTS
		_ = o.stream.WritePacketRTPWithNTP(o.audio, p, now)
	}
	o.audioTS += uint32(len(samples))
	o.audioNext = o.audioNext.Add(time.Duration(len(samples)) * time.Second / audioSampleRate)
}

// replayKeyFrame sends the stored key frame once. The library writes to all
// active viewers; existing viewers simply receive an extra key frame.
func (o *rtspOutput) replayKeyFrame() {
	o.mu.Lock()
	defer o.mu.Unlock()
	if len(o.keys.keyAU) == 0 {
		return
	}
	o.writeAU(o.keys.keyAU, time.Now())
	log.Printf("replayed the last key frame for a new viewer")
}

// replayDelay gives the library time to put the PLAY response on the wire
// before the replayed key frame follows it.
const replayDelay = 20 * time.Millisecond

// rtspHandler serves the single stream on any path.
//
// Key-frame replay: OnPlay runs before the viewer is marked active, so a
// packet written there would be lost. The connection is remembered in OnPlay
// and the replay is triggered from OnResponse, which the library calls after
// the PLAY has been fully processed (the viewer is active by then).
type rtspHandler struct {
	out *rtspOutput

	mu      sync.Mutex
	playing map[*gortsplib.ServerConn]struct{}
}

func (h *rtspHandler) OnConnOpen(ctx *gortsplib.ServerHandlerOnConnOpenCtx) {
	log.Printf("rtsp: client connected from %s", ctx.Conn.NetConn().RemoteAddr())
}

func (h *rtspHandler) OnConnClose(ctx *gortsplib.ServerHandlerOnConnCloseCtx) {
	h.mu.Lock()
	delete(h.playing, ctx.Conn)
	h.mu.Unlock()
	log.Printf("rtsp: client from %s disconnected (%v)", ctx.Conn.NetConn().RemoteAddr(), ctx.Error)
}

func (h *rtspHandler) OnDescribe(*gortsplib.ServerHandlerOnDescribeCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.out.stream, nil
}

func (h *rtspHandler) OnSetup(*gortsplib.ServerHandlerOnSetupCtx) (*base.Response, *gortsplib.ServerStream, error) {
	return &base.Response{StatusCode: base.StatusOK}, h.out.stream, nil
}

func (h *rtspHandler) OnPlay(ctx *gortsplib.ServerHandlerOnPlayCtx) (*base.Response, error) {
	h.mu.Lock()
	if h.playing == nil {
		h.playing = make(map[*gortsplib.ServerConn]struct{})
	}
	h.playing[ctx.Conn] = struct{}{}
	h.mu.Unlock()
	return &base.Response{StatusCode: base.StatusOK}, nil
}

func (h *rtspHandler) OnResponse(sc *gortsplib.ServerConn, res *base.Response) {
	h.mu.Lock()
	_, pending := h.playing[sc]
	delete(h.playing, sc)
	h.mu.Unlock()
	if pending && res.StatusCode == base.StatusOK {
		time.AfterFunc(replayDelay, h.out.replayKeyFrame)
	}
}

// OnAnnounce refuses publishing: this server only serves the camera.
func (h *rtspHandler) OnAnnounce(*gortsplib.ServerHandlerOnAnnounceCtx) (*base.Response, error) {
	return &base.Response{StatusCode: base.StatusForbidden}, nil
}
