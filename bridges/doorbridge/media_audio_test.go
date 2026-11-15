package main

import (
	"bytes"
	"encoding/binary"
	"testing"
)

func TestClassifyFrame(t *testing.T) {
	// JPEG without end marker is not usable.
	if k, _ := classifyFrame([]byte{0xFF, 0xD8, 1, 2, 3}); k != frameUnknown {
		t.Error("JPEG without FF D9 accepted")
	}
	// Three-byte start code after some junk.
	k, d := classifyFrame([]byte{9, 9, 9, 0, 0, 1, 0x41, 7})
	if k != frameH264 || !bytes.Equal(d, []byte{0, 0, 1, 0x41, 7}) {
		t.Errorf("3-byte start code: %d %x", k, d)
	}
	// Start code beyond the first 12 bytes is not accepted.
	late := append(make([]byte, 13), 0, 0, 0, 1, 0x65)
	for i := range late[:13] {
		late[i] = 0xAA
	}
	if k, _ := classifyFrame(late); k != frameUnknown {
		t.Error("late start code accepted")
	}
}

func TestSplitAnnexB(t *testing.T) {
	in := []byte{0, 0, 0, 1, 0x67, 1, 0, 0, 0, 0, 1, 0x68, 2, 0, 0, 1, 0x41, 3, 4}
	got := splitAnnexB(in)
	want := [][]byte{{0x67, 1}, {0x68, 2}, {0x41, 3, 4}}
	if len(got) != len(want) {
		t.Fatalf("got %x", got)
	}
	for i := range want {
		if !bytes.Equal(got[i], want[i]) {
			t.Fatalf("nalu %d = %x, want %x", i, got[i], want[i])
		}
	}
}

func TestKeyFrameStore(t *testing.T) {
	var k keyFrameStore
	sps, pps := []byte{0x67, 1}, []byte{0x68, 2}
	if !k.observe([][]byte{sps, pps, {0x65, 3}, {0x41, 4}}) {
		t.Fatal("parameter sets not reported as changed")
	}
	if len(k.keyAU) != 4 || naluType(k.keyAU[2]) != naluIDR || naluType(k.keyAU[3]) != naluSlice {
		t.Fatalf("key AU %x", k.keyAU)
	}
	if k.observe([][]byte{{0x41, 5}}) {
		t.Fatal("P frame reported a change")
	}
	if len(k.keyAU) != 4 || k.keyAU[2][1] != 3 {
		t.Fatal("P frame replaced the key frame")
	}
	// IDR without parameter sets: the stored ones are prepended.
	k.observe([][]byte{{0x65, 9}})
	if len(k.keyAU) != 3 || naluType(k.keyAU[0]) != naluSPS || naluType(k.keyAU[1]) != naluPPS || k.keyAU[2][1] != 9 {
		t.Fatalf("key AU %x", k.keyAU)
	}
}

func TestIMADecodeKnownVector(t *testing.T) {
	blk := make([]byte, audioBlockLen)
	// predictor 0, step index 0; codes (low nibble first): 7, 0, 8, 8, 0, 0, ...
	blk[4] = 0x07
	blk[5] = 0x88
	out := decodeIMABlock(blk)
	if len(out) != 1025 {
		t.Fatalf("%d samples, want 1025", len(out))
	}
	// Worked by hand with the standard tables:
	// s1: step 7, code 7 -> diff 0+7+3+1 = 11, index 8
	// s2: step 16, code 0 -> diff 2 -> 13, index 7
	// s3: step 14, code 8 -> diff 1 -> 12, index 6
	// s4: step 13, code 8 -> diff 1 -> 11, index 5
	// s5: step 12, code 0 -> diff 1 -> 12, index 4
	// s6: step 11, code 0 -> diff 1 -> 13, index 3
	want := []int16{0, 11, 13, 12, 11, 12, 13}
	for i, w := range want {
		if out[i] != w {
			t.Fatalf("sample %d = %d, want %d (all: %v)", i, out[i], w, out[:7])
		}
	}

	// Step index clamped to 88; predictor clamped to int16 range.
	blk = make([]byte, audioBlockLen)
	binary.LittleEndian.PutUint16(blk[0:], uint16(int16(32000)))
	blk[2] = 200
	blk[4] = 0x07 // code 7 at step 32767 would overflow
	out = decodeIMABlock(blk)
	if out[0] != 32000 || out[1] != 32767 {
		t.Fatalf("clamp: %v", out[:2])
	}
	if decodeIMABlock(make([]byte, 100)) != nil {
		t.Fatal("wrong-size block decoded")
	}
}

// muLawToLinear is the standard G.711 decoder, used to check the encoder.
func muLawToLinear(u byte) int {
	u = ^u
	sign := u & 0x80
	exp := int(u>>4) & 7
	mant := int(u & 0x0F)
	v := ((mant << 3) + 0x84) << exp
	v -= 0x84
	if sign != 0 {
		return -v
	}
	return v
}

func TestMuLaw(t *testing.T) {
	cases := map[int16]byte{
		0:      0xFF,
		32767:  0x80,
		-32768: 0x00,
		100:    0xF2,
		-100:   0x72,
		1000:   0xCE,
	}
	for in, want := range cases {
		if got := linearToMuLaw(in); got != want {
			t.Errorf("linearToMuLaw(%d) = %#x, want %#x", in, got, want)
		}
	}
	// Round trip error must stay within one quantisation step.
	for s := -32000; s <= 32000; s += 37 {
		back := muLawToLinear(linearToMuLaw(int16(s)))
		diff := back - s
		if diff < 0 {
			diff = -diff
		}
		limit := 8 + abs(s)/15 // one quantisation step of this simple decoder
		if diff > limit {
			t.Fatalf("round trip %d -> %d (error %d)", s, back, diff)
		}
	}
}

func abs(v int) int {
	if v < 0 {
		return -v
	}
	return v
}

func TestWAVHeader(t *testing.T) {
	h := wavStreamHeader()
	if len(h) != 44 || string(h[0:4]) != "RIFF" || string(h[8:16]) != "WAVEfmt " || string(h[36:40]) != "data" {
		t.Fatalf("header %x", h)
	}
	if binary.LittleEndian.Uint32(h[4:]) != 0xFFFFFFFF || binary.LittleEndian.Uint32(h[40:]) != 0xFFFFFFFF {
		t.Fatal("sizes must be 0xFFFFFFFF")
	}
	if binary.LittleEndian.Uint32(h[24:]) != 8000 || binary.LittleEndian.Uint16(h[22:]) != 1 || binary.LittleEndian.Uint16(h[34:]) != 16 {
		t.Fatal("format fields wrong")
	}
	if !bytes.Equal(pcmBytes([]int16{1, -2}), []byte{1, 0, 0xFE, 0xFF}) {
		t.Fatal("PCM encoding wrong")
	}
}
