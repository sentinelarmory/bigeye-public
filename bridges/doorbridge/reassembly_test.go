package main

import (
	"bytes"
	"encoding/binary"
	"testing"
	"time"
)

// mkMedia builds a kind-3 packet. hdrLen is the payload offset (24 for a
// plain packet, 40 for packet 0 of a video frame, 28 for audio). For
// hdrLen >= 40 the frame header (count, width, height) is filled in.
func mkMedia(frame, packet, mediaType uint16, hdrLen, count, w, h int, payload []byte) []byte {
	b := make([]byte, hdrLen+len(payload))
	copy(b, "2TEG")
	binary.LittleEndian.PutUint16(b[4:], kindMedia)
	binary.LittleEndian.PutUint16(b[6:], uint16(len(b)-8))
	binary.LittleEndian.PutUint16(b[14:], mediaType)
	binary.LittleEndian.PutUint16(b[16:], frame)
	binary.LittleEndian.PutUint16(b[18:], packet)
	binary.LittleEndian.PutUint32(b[20:], uint32(len(payload)))
	if hdrLen >= 40 {
		binary.LittleEndian.PutUint32(b[28:], uint32(count))
		binary.LittleEndian.PutUint16(b[32:], uint16(w))
		binary.LittleEndian.PutUint16(b[34:], uint16(h))
	}
	copy(b[hdrLen:], payload)
	return b
}

func mustParse(t *testing.T, b []byte) mediaPacket {
	t.Helper()
	p, err := parseMediaPacket(b)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	return p
}

func TestParseMediaHeader(t *testing.T) {
	p := mustParse(t, mkMedia(77, 0, 2, 40, 3, 640, 480, []byte{1, 2, 3}))
	if p.frame != 77 || p.packet != 0 || p.mediaType != 2 || p.count != 3 || p.width != 640 || p.height != 480 {
		t.Fatalf("packet 0 parsed wrong: %+v", p)
	}
	if !bytes.Equal(p.payload, []byte{1, 2, 3}) || p.offset != 40 {
		t.Fatalf("payload wrong: %x at %d", p.payload, p.offset)
	}

	p = mustParse(t, mkMedia(77, 4, 2, 24, 0, 0, 0, []byte{9}))
	if p.packet != 4 || p.count != 0 || p.offset != 24 {
		t.Fatalf("plain packet parsed wrong: %+v", p)
	}

	// Untrusted headers: count out of range, implausible size.
	if p := mustParse(t, mkMedia(1, 0, 2, 40, 100, 640, 480, []byte{1})); p.count != 0 {
		t.Fatal("count 100 trusted")
	}
	if p := mustParse(t, mkMedia(1, 0, 2, 40, 3, 100, 480, []byte{1})); p.count != 0 {
		t.Fatal("width 100 trusted")
	}
	if p := mustParse(t, mkMedia(1, 0, 2, 40, 3, 640, 2200, []byte{1})); p.count != 0 {
		t.Fatal("height 2200 trusted")
	}

	// Audio block.
	p = mustParse(t, mkMedia(5, 0, 1, 28, 0, 0, 0, make([]byte, 516)))
	if !p.isAudio() {
		t.Fatal("audio block not recognised")
	}
	if mustParse(t, mkMedia(5, 0, 1, 28, 0, 0, 0, make([]byte, 515))).isAudio() {
		t.Fatal("515-byte block taken for audio")
	}

	// Rejections.
	bad := mkMedia(1, 1, 2, 24, 0, 0, 0, []byte{1, 2})
	binary.LittleEndian.PutUint32(bad[20:], 0)
	if _, err := parseMediaPacket(bad); err == nil {
		t.Error("P = 0 accepted")
	}
	bad = mkMedia(1, 1, 2, 24, 0, 0, 0, []byte{1, 2})
	binary.LittleEndian.PutUint32(bad[20:], 10) // offset 8+18-10 = 16 < 24
	if _, err := parseMediaPacket(bad); err == nil {
		t.Error("offset below 24 accepted")
	}
	bad = mkMedia(1, 1, 2, 24, 0, 0, 0, []byte{1, 2})
	binary.LittleEndian.PutUint16(bad[6:], 40) // claims more data than present
	if _, err := parseMediaPacket(bad); err == nil {
		t.Error("payload past the end accepted")
	}
}

func TestReassembleJPEGOutOfOrder(t *testing.T) {
	jpeg := []byte{0xFF, 0xD8, 0xAA, 0xFF, 0xD9, 0xBB, 0xFF, 0xD9}
	padded := append(append([]byte{}, jpeg...), 0, 0, 0, 0)
	parts := [][]byte{padded[:5], padded[5:9], padded[9:]}

	a := newAssembler()
	t0 := time.Unix(1000, 0)
	ev := a.push(mustParse(t, mkMedia(10, 2, 2, 24, 0, 0, 0, parts[2])), t0)
	if ev.frame != nil {
		t.Fatal("frame completed too early")
	}
	ev = a.push(mustParse(t, mkMedia(10, 0, 2, 40, 3, 640, 480, parts[0])), t0)
	if ev.frame != nil {
		t.Fatal("frame completed too early")
	}
	ev = a.push(mustParse(t, mkMedia(10, 1, 2, 24, 0, 0, 0, parts[1])), t0)
	if !bytes.Equal(ev.frame, padded) || !ev.sendNow {
		t.Fatalf("frame = %x sendNow=%v", ev.frame, ev.sendNow)
	}
	kind, data := classifyFrame(ev.frame)
	if kind != frameJPEG || !bytes.Equal(data, jpeg) {
		t.Fatalf("JPEG trim: kind %d data %x", kind, data)
	}
	items := a.collect(t0)
	if len(items) != 1 || items[0] != frameAck(10) {
		t.Fatalf("items %v", items)
	}

	// A resent packet of the finished frame: acknowledged again, dropped.
	ev = a.push(mustParse(t, mkMedia(10, 1, 2, 24, 0, 0, 0, parts[1])), t0)
	if ev.frame != nil || !ev.duplicated {
		t.Fatal("duplicate not detected")
	}
	if items := a.collect(t0); len(items) != 1 || items[0] != frameAck(10) {
		t.Fatalf("re-ack items %v", items)
	}
	if a.pendingFrames() != 0 {
		t.Fatal("duplicate created a partial frame")
	}
}

func TestReassembleH264(t *testing.T) {
	frame := []byte{0xEE, 0, 0, 0, 1, 0x67, 1, 2, 0, 0, 1, 0x68, 3, 0, 0, 0, 1, 0x65, 4, 5}
	a := newAssembler()
	now := time.Unix(1000, 0)
	a.push(mustParse(t, mkMedia(3, 0, 2, 40, 2, 1280, 720, frame[:8])), now)
	ev := a.push(mustParse(t, mkMedia(3, 1, 2, 24, 0, 0, 0, frame[8:])), now)
	kind, data := classifyFrame(ev.frame)
	if kind != frameH264 || !bytes.Equal(data, frame[1:]) {
		t.Fatalf("kind %d data %x", kind, data)
	}
	nalus := splitAnnexB(data)
	if len(nalus) != 3 || naluType(nalus[0]) != naluSPS || naluType(nalus[1]) != naluPPS || naluType(nalus[2]) != naluIDR {
		t.Fatalf("nalus %x", nalus)
	}
	if !bytes.Equal(nalus[2], []byte{0x65, 4, 5}) {
		t.Fatalf("IDR %x", nalus[2])
	}
}

func TestAudioBlockAckedImmediately(t *testing.T) {
	a := newAssembler()
	now := time.Unix(1000, 0)
	blk := make([]byte, 516)
	blk[0] = 7
	ev := a.push(mustParse(t, mkMedia(500, 0, 1, 28, 0, 0, 0, blk)), now)
	if ev.audio == nil || !ev.sendNow || ev.frame != nil {
		t.Fatalf("audio event %+v", ev)
	}
	if items := a.collect(now); len(items) != 1 || items[0] != frameAck(500) {
		t.Fatalf("items %v", items)
	}
	// A resend is acknowledged again but not played twice.
	ev = a.push(mustParse(t, mkMedia(500, 0, 1, 28, 0, 0, 0, blk)), now)
	if ev.audio != nil {
		t.Fatal("audio block played twice")
	}
	if a.pendingFrames() != 0 {
		t.Fatal("audio became a partial frame")
	}
}

func TestRetransmissionRequests(t *testing.T) {
	a := newAssembler()
	t0 := time.Unix(1000, 0)
	a.push(mustParse(t, mkMedia(20, 0, 2, 40, 4, 640, 480, []byte{1})), t0)
	a.push(mustParse(t, mkMedia(20, 2, 2, 24, 0, 0, 0, []byte{3})), t0)

	if items := a.collect(t0.Add(100 * time.Millisecond)); len(items) != 0 {
		t.Fatalf("requests before 240 ms: %v", items)
	}
	items := a.collect(t0.Add(240 * time.Millisecond))
	want := []ackItem{packetRequest(2, 20, 1), packetRequest(2, 20, 3)}
	if len(items) != 2 || items[0] != want[0] || items[1] != want[1] {
		t.Fatalf("round 1: %v", items)
	}
	if items := a.collect(t0.Add(400 * time.Millisecond)); len(items) != 0 {
		t.Fatalf("second round too early: %v", items)
	}
	if items := a.collect(t0.Add(480 * time.Millisecond)); len(items) != 2 {
		t.Fatalf("round 2: %v", items)
	}

	// Unknown packet count: ask up to highest seen + 3.
	b := newAssembler()
	b.push(mustParse(t, mkMedia(30, 1, 2, 24, 0, 0, 0, []byte{1})), t0)
	b.push(mustParse(t, mkMedia(30, 5, 2, 24, 0, 0, 0, []byte{1})), t0)
	items = b.collect(t0.Add(300 * time.Millisecond))
	var got []uint16
	for _, it := range items {
		got = append(got, it[3])
	}
	wantPkts := []uint16{0, 2, 3, 4, 6, 7}
	if len(got) != len(wantPkts) {
		t.Fatalf("unknown count requests %v", got)
	}
	for i := range got {
		if got[i] != wantPkts[i] {
			t.Fatalf("unknown count requests %v, want %v", got, wantPkts)
		}
	}

	// At most 64 packet numbers per frame per round.
	c := newAssembler()
	c.push(mustParse(t, mkMedia(40, 0, 2, 40, 99, 640, 480, []byte{1})), t0)
	if items := c.collect(t0.Add(300 * time.Millisecond)); len(items) != 64 || items[0][3] != 1 || items[63][3] != 64 {
		t.Fatalf("per-round cap: %d items", len(items))
	}
}

func TestAbandonIncompleteFrame(t *testing.T) {
	a := newAssembler()
	t0 := time.Unix(1000, 0)
	a.push(mustParse(t, mkMedia(50, 0, 2, 40, 2, 640, 480, []byte{1})), t0)
	rounds := 0
	for ms := 240; ms < 4000; ms += 240 {
		if items := a.collect(t0.Add(time.Duration(ms) * time.Millisecond)); len(items) > 0 {
			rounds++
		}
	}
	if rounds > nakMaxRounds {
		t.Fatalf("%d rounds", rounds)
	}
	items := a.collect(t0.Add(4 * time.Second))
	if len(items) != 1 || items[0] != frameAck(50) || a.pendingFrames() != 0 {
		t.Fatalf("not abandoned: items %v pending %d", items, a.pendingFrames())
	}
	// Late packets of the abandoned frame are acknowledged and dropped.
	ev := a.push(mustParse(t, mkMedia(50, 1, 2, 24, 0, 0, 0, []byte{2})), t0.Add(5*time.Second))
	if !ev.duplicated || ev.frame != nil {
		t.Fatal("late packet of abandoned frame not dropped")
	}
}

func TestAckEveryFifteenPackets(t *testing.T) {
	a := newAssembler()
	now := time.Unix(1000, 0)
	var ev assemblerEvent
	for i := 0; i < 15; i++ {
		ev = a.push(mustParse(t, mkMedia(60, uint16(i+1), 2, 24, 0, 0, 0, []byte{1})), now)
		if i < 14 && ev.sendNow {
			t.Fatalf("sendNow after %d packets", i+1)
		}
	}
	if !ev.sendNow {
		t.Fatal("no sendNow after 15 packets")
	}
}

func TestFrameRateEstimate(t *testing.T) {
	a := newAssembler()
	t0 := time.Unix(1000, 0)
	if a.fps(t0) != 15 {
		t.Fatal("initial fps should be 15")
	}
	for i := 0; i < 30; i++ {
		at := t0.Add(time.Duration(i) * 100 * time.Millisecond)
		a.push(mustParse(t, mkMedia(uint16(i), 0, 2, 40, 1, 640, 480, []byte{1})), at)
	}
	if got := a.fps(t0.Add(2950 * time.Millisecond)); got != 10 {
		t.Fatalf("fps = %d, want 10", got)
	}
	if got := a.fps(t0.Add(time.Minute)); got != 1 {
		t.Fatalf("idle fps = %d, want 1", got)
	}
}

func TestRecentSetForgets(t *testing.T) {
	s := newRecentSet(3)
	for _, v := range []uint16{1, 2, 3, 4} {
		s.add(v)
	}
	if s.contains(1) || !s.contains(2) || !s.contains(4) {
		t.Fatal("ring did not forget the oldest entry")
	}
}
