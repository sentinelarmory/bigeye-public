package main

// Camera status and the latest messages from the camera (SPEC.md section 11).
// The state lives for the whole process; sessions update it.

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math"
	"strconv"
	"sync"
	"time"
)

const sleepConfigLen = 88

// statusReport is the periodic battery report (cmd 13, sub 9).
type statusReport struct {
	batteryPct int
	charging   bool
	batteryMV  int
}

// parseStatusReport parses a kind-1, 48-byte, cmd 13 sub 9 packet.
func parseStatusReport(m controlMessage) (statusReport, bool) {
	if m.kind != kindControl || len(m.raw) != 48 || m.cmd != 13 || m.sub != 9 {
		return statusReport{}, false
	}
	v := binary.LittleEndian.Uint32(m.raw[40:])
	return statusReport{
		batteryPct: int(v & 0xFF),
		charging:   v&0x100 != 0,
		batteryMV:  int(binary.LittleEndian.Uint32(m.raw[44:])),
	}, true
}

// mediaInfo is the answer to the media query (cmd 0x0f, sub 0).
type mediaInfo struct {
	Codec        string `json:"codec"`
	Width        int    `json:"width"`
	Height       int    `json:"height"`
	Profile      uint32 `json:"profile"`
	ReceivedUnix int64  `json:"received_unix"`
}

func parseMediaInfo(m controlMessage) (mediaInfo, bool) {
	if m.cmd != cmdMediaInfo || m.sub != 0 || len(m.payload) < 20 {
		return mediaInfo{}, false
	}
	p := m.payload
	codec := binary.LittleEndian.Uint32(p[0:])
	size := binary.LittleEndian.Uint32(p[12:])
	info := mediaInfo{
		Width:   int(size & 0xFFFF),
		Height:  int(size >> 16),
		Profile: binary.LittleEndian.Uint32(p[16:]),
	}
	switch codec {
	case 0:
		info.Codec = "h264"
	case 1:
		info.Codec = "mjpeg"
	default:
		info.Codec = "unknown-" + strconv.FormatUint(uint64(codec), 10)
	}
	return info, true
}

// sleepConfig is the parsed 88-byte sleep configuration (cmd 0x22, sub 0).
type sleepConfig struct {
	Version          int    `json:"version"`
	LastWakeup       int    `json:"last_wakeup"`
	BatteryPct       int    `json:"battery_pct"`
	PIRLevel         int    `json:"pir_level"`
	BatteryMV        int    `json:"battery_mv"`
	IdleWakeTime     int    `json:"idle_wake_time"`
	Flags            uint32 `json:"flags"`
	SleepEnabled     bool   `json:"sleep_enabled"`
	WakeFromLAN      bool   `json:"wake_from_lan"`
	SleepWhenIdle    bool   `json:"sleep_when_idle"`
	NoSleepOnCharger bool   `json:"no_sleep_on_charger"`
	WakeupTime       uint32 `json:"wakeup_time"`
	WakeupFunc1      uint32 `json:"wakeup_func1"`
	WakeupFunc2      uint32 `json:"wakeup_func2"`
	ReceivedUnix     int64  `json:"received_unix"`
}

// parseSleepConfig returns the parsed config and a copy of the raw 88 bytes.
func parseSleepConfig(m controlMessage) (sleepConfig, []byte, bool) {
	if m.cmd != cmdSleepConfig || m.sub != 0 || len(m.payload) < sleepConfigLen {
		return sleepConfig{}, nil, false
	}
	raw := append([]byte(nil), m.payload[:sleepConfigLen]...)
	flags := binary.LittleEndian.Uint32(raw[8:])
	return sleepConfig{
		Version:          int(raw[0]),
		LastWakeup:       int(raw[1]),
		BatteryPct:       int(raw[2]),
		PIRLevel:         int(raw[3]),
		BatteryMV:        int(binary.LittleEndian.Uint16(raw[4:])),
		IdleWakeTime:     int(binary.LittleEndian.Uint16(raw[6:])),
		Flags:            flags,
		SleepEnabled:     flags&1 != 0,
		WakeFromLAN:      flags&2 != 0,
		SleepWhenIdle:    flags&4 != 0,
		NoSleepOnCharger: flags&8 != 0,
		WakeupTime:       binary.LittleEndian.Uint32(raw[12:]),
		WakeupFunc1:      binary.LittleEndian.Uint32(raw[16:]),
		WakeupFunc2:      binary.LittleEndian.Uint32(raw[20:]),
	}, raw, true
}

// parseNightVision parses the night-vision read reply (cmd 0x13, sub 1).
func parseNightVision(m controlMessage) (bool, bool) {
	if m.cmd != cmdNight || m.sub != 1 || len(m.payload) < 9 {
		return false, false
	}
	return m.payload[8] == 1, true
}

// messageLine formats a camera message for /control/state.
func messageLine(m controlMessage) string {
	return fmt.Sprintf("type%d sub%d %s", m.kind, m.sub, hex.EncodeToString(m.raw[12:]))
}

// cameraState is shared between the session and the HTTP API.
type cameraState struct {
	mu sync.Mutex

	status     statusReport
	statusSeen time.Time
	lastFrame  time.Time

	lastByCmd   map[uint32]string
	media       *mediaInfo
	sleep       *sleepConfig
	sleepRaw    []byte
	nightVision *bool
}

func newCameraState() *cameraState {
	return &cameraState{lastByCmd: make(map[uint32]string)}
}

func (s *cameraState) frameReceived(t time.Time) {
	s.mu.Lock()
	s.lastFrame = t
	s.mu.Unlock()
}

// recordMessage stores a camera message and applies the known ones.
func (s *cameraState) recordMessage(m controlMessage, now time.Time) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.lastByCmd[m.cmd] = messageLine(m)
	if r, ok := parseStatusReport(m); ok {
		s.status = r
		s.statusSeen = now
	}
	if mi, ok := parseMediaInfo(m); ok {
		mi.ReceivedUnix = now.Unix()
		s.media = &mi
	}
	if sc, raw, ok := parseSleepConfig(m); ok {
		sc.ReceivedUnix = now.Unix()
		s.sleep = &sc
		s.sleepRaw = raw
	}
	if on, ok := parseNightVision(m); ok {
		s.nightVision = &on
	}
}

// sleepRawCopy returns a copy of the last sleep config, or nil.
func (s *cameraState) sleepRawCopy() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.sleepRaw == nil {
		return nil
	}
	return append([]byte(nil), s.sleepRaw...)
}

func ageSeconds(t, now time.Time) float64 {
	if t.IsZero() {
		return -1
	}
	return math.Round(now.Sub(t).Seconds()*10) / 10
}

// statusJSON is the body of /status.
type statusJSON struct {
	BatteryPct int     `json:"battery_pct"`
	BatteryMV  int     `json:"battery_mv"`
	Charging   bool    `json:"charging"`
	StatusSeen bool    `json:"status_seen"`
	StatusAge  float64 `json:"status_age_s"`
	FrameSeen  bool    `json:"frame_seen"`
	FrameAge   float64 `json:"frame_age_s"`
}

func (s *cameraState) statusView(now time.Time) statusJSON {
	s.mu.Lock()
	defer s.mu.Unlock()
	return statusJSON{
		BatteryPct: s.status.batteryPct,
		BatteryMV:  s.status.batteryMV,
		Charging:   s.status.charging,
		StatusSeen: !s.statusSeen.IsZero(),
		StatusAge:  ageSeconds(s.statusSeen, now),
		FrameSeen:  !s.lastFrame.IsZero(),
		FrameAge:   ageSeconds(s.lastFrame, now),
	}
}

// controlStateJSON is the body of /control/state.
type controlStateJSON struct {
	LastByCmd   map[string]string `json:"last_by_cmd"`
	Media       *mediaInfo        `json:"media"`
	SleepConfig *sleepConfig      `json:"sleep_config"`
	SleepRaw    string            `json:"sleep_raw"`
	NightVision *bool             `json:"night_vision"`
}

func (s *cameraState) controlView() controlStateJSON {
	s.mu.Lock()
	defer s.mu.Unlock()
	v := controlStateJSON{LastByCmd: make(map[string]string, len(s.lastByCmd))}
	for k, line := range s.lastByCmd {
		v.LastByCmd[strconv.FormatUint(uint64(k), 10)] = line
	}
	if s.media != nil {
		m := *s.media
		v.Media = &m
	}
	if s.sleep != nil {
		c := *s.sleep
		v.SleepConfig = &c
	}
	v.SleepRaw = hex.EncodeToString(s.sleepRaw)
	if s.nightVision != nil {
		b := *s.nightVision
		v.NightVision = &b
	}
	return v
}
