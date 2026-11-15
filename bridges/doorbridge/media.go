package main

// Interpretation of complete frames (SPEC.md section 6, "What a complete
// frame contains"): JPEG trimming for the W2 model and Annex-B handling for
// the door model.

import "bytes"

type frameKind int

const (
	frameUnknown frameKind = iota
	frameJPEG
	frameH264
)

// classifyFrame decides what a complete frame contains and returns the
// cleaned bytes: a JPEG cut after its last end marker, or an Annex-B stream
// starting at its first start code.
func classifyFrame(b []byte) (frameKind, []byte) {
	if len(b) >= 2 && b[0] == 0xFF && b[1] == 0xD8 {
		end := bytes.LastIndex(b, []byte{0xFF, 0xD9})
		if end < 0 {
			return frameUnknown, nil
		}
		return frameJPEG, b[:end+2]
	}
	if start := annexBStart(b); start >= 0 {
		return frameH264, b[start:]
	}
	return frameUnknown, nil
}

// annexBStart returns the offset of the first start code (3 or 4 bytes)
// within the first 12 bytes, or -1.
func annexBStart(b []byte) int {
	limit := 12
	for i := 0; i < limit && i+3 <= len(b); i++ {
		if b[i] == 0 && b[i+1] == 0 {
			if b[i+2] == 1 {
				return i
			}
			if i+4 <= len(b) && b[i+2] == 0 && b[i+3] == 1 {
				return i
			}
		}
	}
	return -1
}

// splitAnnexB splits an Annex-B byte stream into NAL units (without start
// codes). Trailing zero bytes of a NAL unit (before the next start code)
// are dropped. Empty units are skipped.
func splitAnnexB(b []byte) [][]byte {
	var nalus [][]byte
	start := -1
	i := 0
	for i+3 <= len(b) {
		if b[i] == 0 && b[i+1] == 0 && b[i+2] == 1 {
			if start >= 0 {
				nalus = appendNALU(nalus, b[start:i])
			}
			i += 3
			start = i
			continue
		}
		i++
	}
	if start >= 0 {
		nalus = appendNALU(nalus, b[start:])
	}
	return nalus
}

func appendNALU(list [][]byte, n []byte) [][]byte {
	for len(n) > 0 && n[len(n)-1] == 0 {
		n = n[:len(n)-1]
	}
	if len(n) == 0 {
		return list
	}
	return append(list, n)
}

// H.264 NAL unit types used by the bridge.
const (
	naluSlice = 1
	naluIDR   = 5
	naluSPS   = 7
	naluPPS   = 8
)

func naluType(n []byte) int {
	if len(n) == 0 {
		return -1
	}
	return int(n[0] & 0x1F)
}
