package main

import (
	"fmt"
	"net"
	"testing"
	"time"

	"github.com/bluenviron/gortsplib/v4"
	"github.com/bluenviron/gortsplib/v4/pkg/base"
	"github.com/bluenviron/gortsplib/v4/pkg/description"
	"github.com/bluenviron/gortsplib/v4/pkg/format"
	"github.com/pion/rtp"
)

func freeLoopbackPort(t *testing.T) int {
	t.Helper()
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Skipf("no loopback TCP available: %v", err)
	}
	defer l.Close()
	return l.Addr().(*net.TCPAddr).Port
}

// TestKeyFrameReplayOnPlay checks, over loopback only, that a viewer who
// connects after the key frame was sent still receives it right after PLAY.
func TestKeyFrameReplayOnPlay(t *testing.T) {
	port := freeLoopbackPort(t)
	out, err := newRTSPOutput(port)
	if err != nil {
		t.Fatal(err)
	}
	defer out.close()

	// The camera's only key frame arrives before anybody watches.
	// A generic 1920x1080 baseline-profile SPS (the client validates it).
	sps := []byte{0x67, 0x42, 0xc0, 0x28, 0xd9, 0x00, 0x78, 0x02, 0x27, 0xe5, 0x84, 0x00, 0x00, 0x03,
		0x00, 0x04, 0x00, 0x00, 0x03, 0x00, 0xf0, 0x3c, 0x60, 0xc9, 0x20}
	frame := append([]byte{0, 0, 0, 1}, sps...)
	frame = append(frame, 0, 0, 0, 1, 0x68, 0xce, 0x3c, 0x80, 0, 0, 0, 1, 0x65, 0x88, 0x84)
	out.writeVideo(frame, time.Now())

	tcp := gortsplib.TransportTCP
	c := gortsplib.Client{Transport: &tcp}
	u, err := base.ParseURL(fmt.Sprintf("rtsp://127.0.0.1:%d/cam", port))
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Start(u.Scheme, u.Host); err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	desc, _, err := c.Describe(u)
	if err != nil {
		t.Fatal(err)
	}
	if len(desc.Medias) != 2 {
		t.Fatalf("%d medias", len(desc.Medias))
	}
	var h264 *format.H264
	if desc.FindFormat(&h264) == nil {
		t.Fatal("no H.264 track")
	}
	gotSPS, gotPPS := h264.SafeParams()
	if len(gotSPS) == 0 || len(gotPPS) == 0 {
		t.Fatal("SPS/PPS missing from the SDP")
	}
	var g711 *format.G711
	if desc.FindFormat(&g711) == nil || !g711.MULaw || g711.PayloadType() != 0 {
		t.Fatal("no mu-law track")
	}

	if err := c.SetupAll(desc.BaseURL, desc.Medias); err != nil {
		t.Fatal(err)
	}
	got := make(chan byte, 64)
	c.OnPacketRTPAny(func(_ *description.Media, _ format.Format, pkt *rtp.Packet) {
		if len(pkt.Payload) > 0 && pkt.PayloadType == 96 {
			select {
			case got <- pkt.Payload[0] & 0x1F:
			default:
			}
		}
	})
	if _, err := c.Play(nil); err != nil {
		t.Fatal(err)
	}
	deadline := time.After(3 * time.Second)
	for {
		select {
		case typ := <-got:
			// The access unit is aggregated (STAP-A, 24) or sent as an IDR.
			if typ == 24 || typ == naluIDR || typ == naluSPS {
				return
			}
		case <-deadline:
			t.Fatal("stored key frame was not replayed to the new viewer")
		}
	}
}
