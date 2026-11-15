package main

// Packet layouts of the camera's LAN protocol (SPEC.md sections 2-5 and 7).
// Everything here is pure: builders return byte slices, parsers take byte
// slices. No sockets.

import (
	"encoding/binary"
	"errors"
	"net"
)

var (
	magicDiscovery = []byte("1TEG")
	magicSession   = []byte("2TEG")
)

// 2TEG packet kinds.
const (
	kindHandshake = 0
	kindControl   = 1
	kindReply     = 2
	kindMedia     = 3
	kindAck       = 4
)

const (
	discoveryPort = 10104
	tokenLength   = 12

	// controlHeaderLen is the size of a kind-1 control packet without payload.
	controlHeaderLen = 28
	// maxAckItems is the maximum number of items in one kind-4 packet.
	maxAckItems = 123
)

// Control commands used by the bridge itself.
const (
	cmdOpenOrStream = 0x00
	cmdAudio        = 0x01
	cmdKeepalive    = 0x02
	cmdClose        = 0x03
	cmdResolution   = 0x04
	cmdBrightness   = 0x06
	cmdContrast     = 0x07
	cmdTurn         = 0x08
	cmdKeyframe     = 0x0c
	cmdAlarm        = 0x0d
	cmdMediaInfo    = 0x0f
	cmdNight        = 0x13
	cmdTime         = 0x15
	cmdSleepConfig  = 0x22
	cmdGoingToSleep = 0x23
	cmdReboot       = 0x50
)

func hasMagic(b, magic []byte) bool {
	return len(b) >= 4 && string(b[:4]) == string(magic)
}

// buildDiscoveryRequest returns the 20-byte discovery request (section 3).
func buildDiscoveryRequest() []byte {
	b := make([]byte, 20)
	copy(b, magicDiscovery)
	binary.LittleEndian.PutUint16(b[4:], 11)
	binary.LittleEndian.PutUint16(b[6:], 12)
	binary.LittleEndian.PutUint16(b[8:], 1)
	return b
}

// parseDiscoveryReply extracts the media port from a discovery answer.
// srcPort is the UDP source port of the answer; it is used when the
// advertised port is zero. Returns 0 when no usable port is found.
func parseDiscoveryReply(b []byte, srcPort int) int {
	if !hasMagic(b, magicDiscovery) || len(b) < 36 {
		return 0
	}
	port := int(binary.BigEndian.Uint16(b[34:]))
	if port == 0 && srcPort != discoveryPort {
		port = srcPort
	}
	return port
}

// buildHello returns the 48-byte LAN hello (section 4).
func buildHello(localPort int, localIP net.IP, token string) ([]byte, error) {
	if len(token) != tokenLength {
		return nil, errors.New("the LAN token must be exactly 12 characters")
	}
	ip4 := localIP.To4()
	if ip4 == nil {
		return nil, errors.New("the advertised local address must be IPv4")
	}
	b := make([]byte, 48)
	copy(b, magicDiscovery)
	binary.LittleEndian.PutUint16(b[4:], 13)
	binary.LittleEndian.PutUint16(b[6:], 40)
	binary.BigEndian.PutUint16(b[26:], uint16(localPort))
	copy(b[28:32], ip4)
	b[32] = 0x09
	copy(b[33:45], token)
	return b, nil
}

// parseHelloReply returns the session id if b is an acceptance of the hello.
func parseHelloReply(b []byte) (uint32, bool) {
	switch {
	case hasMagic(b, magicDiscovery):
		if len(b) < 28 || binary.LittleEndian.Uint16(b[4:]) != 14 {
			return 0, false
		}
		id := binary.LittleEndian.Uint32(b[24:])
		return id, id != 0
	case hasMagic(b, magicSession):
		if len(b) < 12 {
			return 0, false
		}
		id := binary.LittleEndian.Uint32(b[8:])
		return id, id != 0
	}
	return 0, false
}

// buildControl returns a kind-1 control packet (section 5).
func buildControl(session, cmd, sub uint32, payload []byte) []byte {
	b := make([]byte, controlHeaderLen+len(payload))
	copy(b, magicSession)
	binary.LittleEndian.PutUint16(b[4:], kindControl)
	binary.LittleEndian.PutUint16(b[6:], uint16(20+len(payload)))
	binary.LittleEndian.PutUint32(b[8:], session)
	binary.LittleEndian.PutUint32(b[12:], cmd)
	binary.LittleEndian.PutUint32(b[16:], sub)
	binary.LittleEndian.PutUint32(b[20:], uint32(len(payload)))
	// b[24:28] stays zero.
	copy(b[28:], payload)
	return b
}

// streamPayload is the body of the start/stop stream request.
func streamPayload(mode uint32) []byte {
	p := make([]byte, 8)
	binary.LittleEndian.PutUint32(p[4:], mode)
	return p
}

// keepalivePayload is the body of the cmd-2 keepalive (play rate in fps).
func keepalivePayload(fps uint16) []byte {
	p := make([]byte, 8)
	binary.LittleEndian.PutUint16(p[4:], fps)
	return p
}

// controlStep is one control packet without the session id.
type controlStep struct {
	cmd, sub uint32
	payload  []byte
}

// bootstrapSteps returns the packets sent right after the hello, in order.
func bootstrapSteps(streamMode uint32) []controlStep {
	return []controlStep{
		{cmd: 0, sub: 1, payload: make([]byte, 8)},
		{cmd: 13, sub: 1},
		{cmd: 14, sub: 2},
		{cmd: 15, sub: 0},
		{cmd: 15, sub: 2},
		{cmd: 21, sub: 0},
		{cmd: 19, sub: 1},
		{cmd: 26, sub: 0},
		{cmd: 0, sub: 1, payload: streamPayload(streamMode)},
	}
}

// ackItem is one 8-byte entry of a kind-4 packet.
type ackItem [4]uint16

func frameAck(frame uint16) ackItem { return ackItem{0x0201, 0, frame, 0xFFFF} }

func packetRequest(mediaType, frame, packet uint16) ackItem {
	return ackItem{0, mediaType, frame, packet}
}

// buildAcks packs items into kind-4 packets of at most maxAckItems items
// each. It returns nil for an empty list (an empty packet is never sent).
func buildAcks(session uint32, fps int, items []ackItem) [][]byte {
	if fps < 1 {
		fps = 1
	}
	if fps > 255 {
		fps = 255
	}
	var out [][]byte
	for len(items) > 0 {
		n := len(items)
		if n > maxAckItems {
			n = maxAckItems
		}
		b := make([]byte, 16+8*n)
		copy(b, magicSession)
		binary.LittleEndian.PutUint16(b[4:], kindAck)
		binary.LittleEndian.PutUint16(b[6:], uint16(8+8*n))
		binary.LittleEndian.PutUint32(b[8:], session)
		b[12] = byte(fps)
		b[15] = byte(n)
		for i, it := range items[:n] {
			o := 16 + 8*i
			for j, v := range it {
				binary.LittleEndian.PutUint16(b[o+2*j:], v)
			}
		}
		out = append(out, b)
		items = items[n:]
	}
	return out
}

// sessionHeader is the common 8-byte header of 2TEG packets.
type sessionHeader struct {
	kind   uint16
	length uint16
}

func parseSessionHeader(b []byte) (sessionHeader, bool) {
	if !hasMagic(b, magicSession) || len(b) < 8 {
		return sessionHeader{}, false
	}
	return sessionHeader{
		kind:   binary.LittleEndian.Uint16(b[4:]),
		length: binary.LittleEndian.Uint16(b[6:]),
	}, true
}

// controlMessage is a kind-1 or kind-2 packet from the camera.
type controlMessage struct {
	kind    uint16
	cmd     uint32
	sub     uint32
	raw     []byte // the whole packet
	payload []byte // bytes from offset 28 on (may be empty)
}

func parseControlMessage(b []byte) (controlMessage, bool) {
	h, ok := parseSessionHeader(b)
	if !ok || (h.kind != kindControl && h.kind != kindReply) || len(b) < 20 {
		return controlMessage{}, false
	}
	m := controlMessage{
		kind: h.kind,
		cmd:  binary.LittleEndian.Uint32(b[12:]),
		sub:  binary.LittleEndian.Uint32(b[16:]),
		raw:  b,
	}
	if len(b) > controlHeaderLen {
		m.payload = b[controlHeaderLen:]
	}
	return m, true
}
