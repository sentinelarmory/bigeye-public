package main

import (
	"bytes"
	"encoding/hex"
	"net"
	"testing"
)

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatalf("bad hex in test: %v", err)
	}
	return b
}

func TestDiscoveryRequestBytes(t *testing.T) {
	want := mustHex(t, "31544547"+"0b00"+"0c00"+"0100"+"00000000000000000000")
	if got := buildDiscoveryRequest(); !bytes.Equal(got, want) {
		t.Fatalf("discovery request\n got %x\nwant %x", got, want)
	}
}

func TestParseDiscoveryReply(t *testing.T) {
	b := make([]byte, 76)
	copy(b, "1TEG")
	b[34], b[35] = 0x9c, 0x40 // 40000 big-endian
	if p := parseDiscoveryReply(b, 10104); p != 40000 {
		t.Fatalf("port = %d, want 40000", p)
	}
	b[34], b[35] = 0, 0
	if p := parseDiscoveryReply(b, 32100); p != 32100 {
		t.Fatalf("zero port should fall back to source port, got %d", p)
	}
	if p := parseDiscoveryReply(b, discoveryPort); p != 0 {
		t.Fatalf("source port 10104 must not be used, got %d", p)
	}
	if p := parseDiscoveryReply(b[:35], 32100); p != 0 {
		t.Fatalf("short answer accepted")
	}
	copy(b, "2TEG")
	if p := parseDiscoveryReply(b, 32100); p != 0 {
		t.Fatalf("wrong magic accepted")
	}
}

func TestHelloBytes(t *testing.T) {
	got, err := buildHello(36891, net.IPv4(192, 168, 1, 50), "ABCDEFGHIJKL")
	if err != nil {
		t.Fatal(err)
	}
	want := mustHex(t, "31544547"+"0d00"+"2800"+
		"000000000000000000000000000000000000"+ // 18 zero bytes (8..25)
		"901b"+ // 36891 big-endian
		"c0a80132"+
		"09"+
		hex.EncodeToString([]byte("ABCDEFGHIJKL"))+
		"000000")
	if !bytes.Equal(got, want) {
		t.Fatalf("hello\n got %x\nwant %x", got, want)
	}
	if len(got) != 48 {
		t.Fatalf("hello length %d", len(got))
	}
	if _, err := buildHello(1, net.IPv4(1, 2, 3, 4), "short"); err == nil {
		t.Fatal("short token accepted")
	}
}

func TestParseHelloReply(t *testing.T) {
	a := make([]byte, 28)
	copy(a, "1TEG")
	a[4] = 14
	a[24] = 2
	if id, ok := parseHelloReply(a); !ok || id != 2 {
		t.Fatalf("1TEG kind 14: id=%d ok=%v", id, ok)
	}
	a[4] = 13
	if _, ok := parseHelloReply(a); ok {
		t.Fatal("1TEG kind 13 accepted")
	}
	b := make([]byte, 12)
	copy(b, "2TEG")
	b[8] = 1
	if id, ok := parseHelloReply(b); !ok || id != 1 {
		t.Fatalf("2TEG: id=%d ok=%v", id, ok)
	}
	b[8] = 0
	if _, ok := parseHelloReply(b); ok {
		t.Fatal("session id 0 accepted")
	}
}

func TestControlPacketBytes(t *testing.T) {
	// First bootstrap step: cmd 0 sub 1 with an 8-byte zero body.
	st := bootstrapSteps(3)[0]
	got := buildControl(1, st.cmd, st.sub, st.payload)
	want := mustHex(t, "32544547"+"0100"+"1c00"+"01000000"+"00000000"+"01000000"+"08000000"+"00000000"+"0000000000000000")
	if !bytes.Equal(got, want) {
		t.Fatalf("open\n got %x\nwant %x", got, want)
	}

	// No payload: cmd 13 sub 1.
	got = buildControl(7, 13, 1, nil)
	want = mustHex(t, "32544547"+"0100"+"1400"+"07000000"+"0d000000"+"01000000"+"00000000"+"00000000")
	if !bytes.Equal(got, want) {
		t.Fatalf("cmd13\n got %x\nwant %x", got, want)
	}

	// Start stream, mode 3.
	last := bootstrapSteps(3)[8]
	got = buildControl(1, last.cmd, last.sub, last.payload)
	if !bytes.Equal(got[28:], mustHex(t, "0000000003000000")) || got[12] != 0 || got[16] != 1 {
		t.Fatalf("start stream %x", got)
	}

	// Keepalive: cmd 2, payload u32 0, u16 15, u16 0.
	got = buildControl(1, cmdKeepalive, 0, keepalivePayload(15))
	if !bytes.Equal(got[12:], mustHex(t, "02000000"+"00000000"+"08000000"+"00000000"+"000000000f000000")) {
		t.Fatalf("keepalive %x", got)
	}
}

func TestBootstrapOrder(t *testing.T) {
	want := [][2]uint32{{0, 1}, {13, 1}, {14, 2}, {15, 0}, {15, 2}, {21, 0}, {19, 1}, {26, 0}, {0, 1}}
	steps := bootstrapSteps(3)
	if len(steps) != len(want) {
		t.Fatalf("%d steps", len(steps))
	}
	for i, w := range want {
		if steps[i].cmd != w[0] || steps[i].sub != w[1] {
			t.Errorf("step %d = cmd %d sub %d, want %v", i, steps[i].cmd, steps[i].sub, w)
		}
	}
}

func TestAckPacketBytes(t *testing.T) {
	items := []ackItem{frameAck(0x1234), packetRequest(2, 0x1235, 5)}
	pkts := buildAcks(1, 15, items)
	if len(pkts) != 1 {
		t.Fatalf("%d packets", len(pkts))
	}
	want := mustHex(t, "32544547"+"0400"+"1800"+"01000000"+"0f"+"0000"+"02"+
		"0102"+"0000"+"3412"+"ffff"+
		"0000"+"0200"+"3512"+"0500")
	if !bytes.Equal(pkts[0], want) {
		t.Fatalf("ack\n got %x\nwant %x", pkts[0], want)
	}

	if buildAcks(1, 15, nil) != nil {
		t.Fatal("empty ack packet built")
	}

	many := make([]ackItem, 130)
	pkts = buildAcks(1, 999, many)
	if len(pkts) != 2 || pkts[0][15] != 123 || pkts[1][15] != 7 {
		t.Fatalf("split wrong: %d packets", len(pkts))
	}
	if pkts[0][12] != 255 {
		t.Fatalf("fps not clamped: %d", pkts[0][12])
	}
	if len(pkts[0]) != 16+8*123 || pkts[0][6] != byte((8+8*123)&0xff) {
		t.Fatalf("length field wrong")
	}
}
