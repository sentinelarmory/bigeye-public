package main

// Media packet parsing, frame reassembly, and the acknowledgement /
// retransmission bookkeeping (SPEC.md sections 6 and 7). The assembler is
// driven by the session goroutine and takes the current time as an argument,
// so it is deterministic and testable.

import (
	"encoding/binary"
	"errors"
	"sort"
	"time"
)

const (
	audioBlockLen       = 516
	audioPayloadOffset  = 28
	videoFirstMinOffset = 40

	nakStartAge     = 240 * time.Millisecond
	nakInterval     = 240 * time.Millisecond
	nakMaxRounds    = 16
	nakMaxPerRound  = 64
	frameMaxAge     = 4 * time.Second
	finishedHistory = 180
	maxPartials     = 96
	audioHistory    = 32
)

// mediaPacket is a parsed kind-3 packet.
type mediaPacket struct {
	mediaType uint16
	frame     uint16
	packet    uint16
	payload   []byte
	offset    int // payload offset inside the packet

	// Only for packet 0 of a video frame with a trusted header.
	count         int
	width, height int
}

func (p mediaPacket) isAudio() bool {
	return p.packet == 0 && p.offset == audioPayloadOffset && len(p.payload) == audioBlockLen
}

// parseMediaPacket parses a kind-3 packet (the 8-byte header included).
func parseMediaPacket(b []byte) (mediaPacket, error) {
	h, ok := parseSessionHeader(b)
	if !ok || h.kind != kindMedia {
		return mediaPacket{}, errors.New("not a media packet")
	}
	if len(b) < 24 {
		return mediaPacket{}, errors.New("media packet too short")
	}
	l := int(h.length)
	p := int(binary.LittleEndian.Uint32(b[20:]))
	off := 8 + l - p
	if p <= 0 || p > len(b) || off < 24 || off+p > len(b) {
		return mediaPacket{}, errors.New("media packet has an invalid payload length")
	}
	mp := mediaPacket{
		mediaType: binary.LittleEndian.Uint16(b[14:]),
		frame:     binary.LittleEndian.Uint16(b[16:]),
		packet:    binary.LittleEndian.Uint16(b[18:]),
		payload:   b[off : off+p],
		offset:    off,
	}
	if mp.packet == 0 && off >= videoFirstMinOffset {
		count := int(binary.LittleEndian.Uint32(b[28:]))
		w := int(binary.LittleEndian.Uint16(b[32:]))
		ht := int(binary.LittleEndian.Uint16(b[34:]))
		if count >= 1 && count <= 99 && w >= 160 && w <= 4096 && ht >= 120 && ht <= 2160 {
			mp.count, mp.width, mp.height = count, w, ht
		}
	}
	return mp, nil
}

// partialFrame is a video frame that is still being received.
type partialFrame struct {
	firstSeen time.Time
	mediaType uint16
	count     int // 0 = not known yet
	maxSeen   int
	parts     map[uint16][]byte
	lastNak   time.Time
	rounds    int
}

func (f *partialFrame) complete() bool {
	if f.count == 0 || len(f.parts) < f.count {
		return false
	}
	for i := 0; i < f.count; i++ {
		if _, ok := f.parts[uint16(i)]; !ok {
			return false
		}
	}
	return true
}

func (f *partialFrame) assemble() []byte {
	n := 0
	for i := 0; i < f.count; i++ {
		n += len(f.parts[uint16(i)])
	}
	out := make([]byte, 0, n)
	for i := 0; i < f.count; i++ {
		out = append(out, f.parts[uint16(i)]...)
	}
	return out
}

// recentSet remembers the last N frame numbers.
type recentSet struct {
	ring []uint16
	pos  int
	has  map[uint16]int // number -> occurrences in ring
}

func newRecentSet(n int) *recentSet {
	return &recentSet{ring: make([]uint16, 0, n), has: make(map[uint16]int)}
}

func (s *recentSet) contains(v uint16) bool { return s.has[v] > 0 }

func (s *recentSet) add(v uint16) {
	if len(s.ring) < cap(s.ring) {
		s.ring = append(s.ring, v)
	} else {
		old := s.ring[s.pos]
		if s.has[old]--; s.has[old] <= 0 {
			delete(s.has, old)
		}
		s.ring[s.pos] = v
		s.pos = (s.pos + 1) % cap(s.ring)
	}
	s.has[v]++
}

// assemblerEvent is what feeding a packet produced.
type assemblerEvent struct {
	frame      []byte // a complete video frame, or nil
	audio      []byte // a 516-byte audio block, or nil
	sendNow    bool   // a kind-4 packet should be sent immediately
	duplicated bool   // the packet belonged to an already finished frame
}

// assembler reassembles frames and tracks what must be acknowledged.
type assembler struct {
	partials    map[uint16]*partialFrame
	finished    *recentSet
	audioSeen   *recentSet
	pendingAcks []ackItem
	sinceSend   int // media packets received since the last kind-4 packet
	lastSend    time.Time
	completions []time.Time // completion times within the last second
	started     time.Time   // first completion (for the frame-rate estimate)
	abandoned   int
}

func newAssembler() *assembler {
	return &assembler{
		partials:  make(map[uint16]*partialFrame),
		finished:  newRecentSet(finishedHistory),
		audioSeen: newRecentSet(audioHistory),
	}
}

// push feeds one media packet into the assembler.
func (a *assembler) push(p mediaPacket, now time.Time) assemblerEvent {
	var ev assemblerEvent
	a.sinceSend++
	if a.sinceSend >= 15 {
		ev.sendNow = true
	}

	if p.isAudio() {
		a.pendingAcks = append(a.pendingAcks, frameAck(p.frame))
		ev.sendNow = true
		if !a.audioSeen.contains(p.frame) {
			a.audioSeen.add(p.frame)
			ev.audio = p.payload
		}
		return ev
	}

	if a.finished.contains(p.frame) {
		a.queueAck(p.frame)
		ev.duplicated = true
		return ev
	}

	f := a.partials[p.frame]
	if f == nil {
		a.evictIfFull()
		f = &partialFrame{firstSeen: now, mediaType: p.mediaType, parts: make(map[uint16][]byte)}
		a.partials[p.frame] = f
	}
	if p.count > 0 {
		f.count = p.count
	}
	if int(p.packet) > f.maxSeen {
		f.maxSeen = int(p.packet)
	}
	if _, ok := f.parts[p.packet]; !ok {
		f.parts[p.packet] = append([]byte(nil), p.payload...)
	}

	if f.complete() {
		ev.frame = f.assemble()
		delete(a.partials, p.frame)
		a.finished.add(p.frame)
		a.queueAck(p.frame)
		a.completions = append(a.completions, now)
		if a.started.IsZero() {
			a.started = now
		}
		ev.sendNow = true
	}
	return ev
}

func (a *assembler) queueAck(frame uint16) {
	for _, it := range a.pendingAcks {
		if it == frameAck(frame) {
			return
		}
	}
	a.pendingAcks = append(a.pendingAcks, frameAck(frame))
}

// evictIfFull drops the oldest incomplete frame when too many are kept.
func (a *assembler) evictIfFull() {
	if len(a.partials) < maxPartials {
		return
	}
	var oldest uint16
	var oldestT time.Time
	first := true
	for n, f := range a.partials {
		if first || f.firstSeen.Before(oldestT) {
			oldest, oldestT, first = n, f.firstSeen, false
		}
	}
	a.abandon(oldest)
}

func (a *assembler) abandon(frame uint16) {
	delete(a.partials, frame)
	a.finished.add(frame)
	a.queueAck(frame)
	a.abandoned++
}

// fps is the frame rate reported in kind-4 packets.
func (a *assembler) fps(now time.Time) int {
	cut := now.Add(-time.Second)
	i := 0
	for i < len(a.completions) && !a.completions[i].After(cut) {
		i++
	}
	a.completions = a.completions[i:]
	if a.started.IsZero() || now.Sub(a.started) < time.Second {
		return 15
	}
	n := len(a.completions)
	if n < 1 {
		n = 1
	}
	if n > 255 {
		n = 255
	}
	return n
}

// pendingFrames is the number of incomplete frames being kept.
func (a *assembler) pendingFrames() int { return len(a.partials) }

// collect returns the items to send now: all pending ACKs plus due
// retransmission requests. It also abandons frames that are too old or have
// used up their retransmission rounds. An empty result means nothing to send.
func (a *assembler) collect(now time.Time) []ackItem {
	frames := make([]uint16, 0, len(a.partials))
	for n := range a.partials {
		frames = append(frames, n)
	}
	sort.Slice(frames, func(i, j int) bool {
		return a.partials[frames[i]].firstSeen.Before(a.partials[frames[j]].firstSeen)
	})

	var naks []ackItem
	for _, n := range frames {
		f := a.partials[n]
		age := now.Sub(f.firstSeen)
		if age >= frameMaxAge {
			a.abandon(n)
			continue
		}
		if age < nakStartAge || (!f.lastNak.IsZero() && now.Sub(f.lastNak) < nakInterval) {
			continue
		}
		if f.rounds >= nakMaxRounds {
			a.abandon(n)
			continue
		}
		limit := f.count
		if limit == 0 {
			limit = f.maxSeen + 3
		}
		asked := 0
		for i := 0; i < limit && asked < nakMaxPerRound; i++ {
			if _, ok := f.parts[uint16(i)]; !ok {
				naks = append(naks, packetRequest(f.mediaType, n, uint16(i)))
				asked++
			}
		}
		if asked > 0 {
			f.rounds++
			f.lastNak = now
		}
	}

	items := append(a.pendingAcks, naks...)
	a.pendingAcks = nil
	a.sinceSend = 0
	if len(items) > 0 {
		a.lastSend = now
	}
	return items
}

// hasPending reports whether anything may need to be sent soon.
func (a *assembler) hasPending() bool {
	return len(a.pendingAcks) > 0 || len(a.partials) > 0
}
