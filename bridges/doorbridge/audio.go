package main

// Audio: IMA ADPCM block decoding, G.711 mu-law encoding, and a fan-out hub
// for decoded PCM (SPEC.md section 8).

import (
	"encoding/binary"
	"sync"
)

const audioSampleRate = 8000

var imaStepTable = [89]int{
	7, 8, 9, 10, 11, 12, 13, 14, 16, 17,
	19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
	50, 55, 60, 66, 73, 80, 88, 97, 107, 118,
	130, 143, 157, 173, 190, 209, 230, 253, 279, 307,
	337, 371, 408, 449, 494, 544, 598, 658, 724, 796,
	876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066,
	2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871, 5358,
	5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
	15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
}

var imaIndexTable = [16]int{
	-1, -1, -1, -1, 2, 4, 6, 8,
	-1, -1, -1, -1, 2, 4, 6, 8,
}

func clampInt(v, lo, hi int) int {
	if v < lo {
		return lo
	}
	if v > hi {
		return hi
	}
	return v
}

// decodeIMABlock decodes one 516-byte camera audio block into 1025 samples.
// It returns nil if the block has the wrong size.
func decodeIMABlock(block []byte) []int16 {
	if len(block) != audioBlockLen {
		return nil
	}
	pred := int(int16(binary.LittleEndian.Uint16(block[0:2])))
	index := clampInt(int(block[2]), 0, 88)
	out := make([]int16, 0, 1+2*(len(block)-4))
	out = append(out, int16(pred))

	step := func(code int) {
		s := imaStepTable[index]
		diff := s >> 3
		if code&4 != 0 {
			diff += s
		}
		if code&2 != 0 {
			diff += s >> 1
		}
		if code&1 != 0 {
			diff += s >> 2
		}
		if code&8 != 0 {
			pred -= diff
		} else {
			pred += diff
		}
		pred = clampInt(pred, -32768, 32767)
		index = clampInt(index+imaIndexTable[code], 0, 88)
		out = append(out, int16(pred))
	}
	for _, b := range block[4:] {
		step(int(b & 0x0F))
		step(int(b >> 4))
	}
	return out
}

// linearToMuLaw converts one 16-bit sample to G.711 mu-law.
func linearToMuLaw(sample int16) byte {
	const bias = 0x84
	const clip = 32635
	s := int(sample)
	sign := 0
	if s < 0 {
		s = -s
		sign = 0x80
	}
	if s > clip {
		s = clip
	}
	s += bias
	exponent := 7
	for mask := 0x4000; s&mask == 0 && exponent > 0; mask >>= 1 {
		exponent--
	}
	mantissa := (s >> (exponent + 3)) & 0x0F
	return ^byte(sign | exponent<<4 | mantissa)
}

// samplesToMuLaw converts a slice of samples to mu-law bytes.
func samplesToMuLaw(samples []int16) []byte {
	out := make([]byte, len(samples))
	for i, s := range samples {
		out[i] = linearToMuLaw(s)
	}
	return out
}

// pcmHub fans decoded PCM blocks out to listeners (the WAV endpoint).
// Slow listeners lose blocks instead of blocking the session.
type pcmHub struct {
	mu   sync.Mutex
	subs map[chan []int16]struct{}
}

func newPCMHub() *pcmHub { return &pcmHub{subs: make(map[chan []int16]struct{})} }

func (h *pcmHub) subscribe() chan []int16 {
	ch := make(chan []int16, 16)
	h.mu.Lock()
	h.subs[ch] = struct{}{}
	h.mu.Unlock()
	return ch
}

func (h *pcmHub) unsubscribe(ch chan []int16) {
	h.mu.Lock()
	delete(h.subs, ch)
	h.mu.Unlock()
}

func (h *pcmHub) publish(samples []int16) {
	h.mu.Lock()
	defer h.mu.Unlock()
	for ch := range h.subs {
		select {
		case ch <- samples:
		default:
		}
	}
}

// wavStreamHeader returns the 44-byte header of an endless 8 kHz mono
// 16-bit PCM WAV stream.
func wavStreamHeader() []byte {
	h := make([]byte, 44)
	copy(h[0:], "RIFF")
	binary.LittleEndian.PutUint32(h[4:], 0xFFFFFFFF)
	copy(h[8:], "WAVE")
	copy(h[12:], "fmt ")
	binary.LittleEndian.PutUint32(h[16:], 16)
	binary.LittleEndian.PutUint16(h[20:], 1) // PCM
	binary.LittleEndian.PutUint16(h[22:], 1) // mono
	binary.LittleEndian.PutUint32(h[24:], audioSampleRate)
	binary.LittleEndian.PutUint32(h[28:], audioSampleRate*2) // byte rate
	binary.LittleEndian.PutUint16(h[32:], 2)                 // block align
	binary.LittleEndian.PutUint16(h[34:], 16)                // bits per sample
	copy(h[36:], "data")
	binary.LittleEndian.PutUint32(h[40:], 0xFFFFFFFF)
	return h
}

// pcmBytes encodes samples as 16-bit little-endian.
func pcmBytes(samples []int16) []byte {
	out := make([]byte, 2*len(samples))
	for i, s := range samples {
		binary.LittleEndian.PutUint16(out[2*i:], uint16(s))
	}
	return out
}
