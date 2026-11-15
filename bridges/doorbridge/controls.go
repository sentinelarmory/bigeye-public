package main

// Camera controls: HTTP parameters -> kind-1 control packets (SPEC.md
// section 10). The builders are pure functions so they can be tested
// without a network.

import (
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

const controlQueueSize = 8

// controlRequest is one queued API action. Every step is sent `repeat`
// times with `gap` between sends.
type controlRequest struct {
	label  string
	steps  []controlStep
	repeat int
	gap    time.Duration
}

// apiError carries an HTTP status with a one-line explanation.
type apiError struct {
	status int
	msg    string
}

func (e *apiError) Error() string { return e.msg }

func badRequest(format string, args ...any) error {
	return &apiError{status: http.StatusBadRequest, msg: fmt.Sprintf(format, args...)}
}

// controlRoute describes one control endpoint.
type controlRoute struct {
	path     string
	postOnly bool
	build    func(q url.Values, st *cameraState, now time.Time) (controlRequest, error)
}

func controlRoutes() []controlRoute {
	return []controlRoute{
		{"/ir", false, buildIRRequest},
		{"/pir", false, buildPIRRequest},
		{"/control/light", false, buildLightRequest},
		{"/control/picture", false, buildPictureRequest},
		{"/control/resolution", false, buildResolutionRequest},
		{"/control/keyframe", true, buildKeyframeRequest},
		{"/control/time", true, buildTimeRequest},
		{"/control/audio", false, buildAudioRequest},
		{"/reboot", true, buildRebootRequest},
		{"/control/query", false, buildQueryRequest},
		{"/control/sleepflags", true, buildSleepFlagsRequest},
		{"/control/sleepraw", true, buildSleepRawRequest},
		{"/control/probe", true, buildProbeRequest},
	}
}

func single(label string, cmd, sub uint32, payload []byte) controlRequest {
	return controlRequest{label: label, steps: []controlStep{{cmd: cmd, sub: sub, payload: payload}}, repeat: 1}
}

// parseOn accepts 1/0, true/false, on/off.
func parseOn(q url.Values) (bool, error) {
	v := strings.ToLower(strings.TrimSpace(q.Get("on")))
	switch v {
	case "1", "true", "on":
		return true, nil
	case "0", "false", "off":
		return false, nil
	case "":
		return false, badRequest("missing parameter on (use on=1 or on=0)")
	}
	return false, badRequest("invalid value for on: %q (use 1/0, true/false or on/off)", v)
}

func onOff(on bool) string {
	if on {
		return "on"
	}
	return "off"
}

func buildIRRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	on, err := parseOn(q)
	if err != nil {
		return controlRequest{}, err
	}
	sub := uint32(4)
	if on {
		sub = 3
	}
	r := single("night vision "+onOff(on), cmdNight, sub, nil)
	r.repeat, r.gap = 3, 20*time.Millisecond
	return r, nil
}

func buildPIRRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	on, err := parseOn(q)
	if err != nil {
		return controlRequest{}, err
	}
	p := make([]byte, 12)
	if on {
		p[0] = 1
	}
	return single("motion detection "+onOff(on), cmdAlarm, 10, p), nil
}

func buildLightRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	on, err := parseOn(q)
	if err != nil {
		return controlRequest{}, err
	}
	p := []byte{0xFF, 0, 0, 0, 0xFF, 0, 0, 0, 0, 0, 0, 0}
	if on {
		p[2] = 1
	}
	r := single("light "+onOff(on), cmdNight, 5, p)
	r.repeat, r.gap = 2, 20*time.Millisecond
	return r, nil
}

func parsePercent(name, v string) (uint32, error) {
	n, err := strconv.Atoi(strings.TrimSpace(v))
	if err != nil || n < 0 || n > 100 {
		return 0, badRequest("%s must be a number from 0 to 100", name)
	}
	return uint32(n), nil
}

func buildPictureRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	r := controlRequest{repeat: 1}
	var labels []string
	if v, ok := q["brightness"]; ok {
		n, err := parsePercent("brightness", v[0])
		if err != nil {
			return controlRequest{}, err
		}
		r.steps = append(r.steps, controlStep{cmd: cmdBrightness, sub: n})
		labels = append(labels, fmt.Sprintf("brightness %d", n))
	}
	if v, ok := q["contrast"]; ok {
		n, err := parsePercent("contrast", v[0])
		if err != nil {
			return controlRequest{}, err
		}
		r.steps = append(r.steps, controlStep{cmd: cmdContrast, sub: n})
		labels = append(labels, fmt.Sprintf("contrast %d", n))
	}
	if v, ok := q["turn"]; ok {
		switch strings.ToLower(v[0]) {
		case "flip":
			r.steps = append(r.steps, controlStep{cmd: cmdTurn, sub: 1})
			labels = append(labels, "flip")
		case "mirror":
			r.steps = append(r.steps, controlStep{cmd: cmdTurn, sub: 2})
			labels = append(labels, "mirror")
		default:
			return controlRequest{}, badRequest("turn must be flip or mirror")
		}
	}
	if len(r.steps) == 0 {
		return controlRequest{}, badRequest("give brightness=N, contrast=N or turn=flip|mirror")
	}
	r.label = "picture " + strings.Join(labels, ", ")
	return r, nil
}

func buildResolutionRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	size := strings.ToLower(strings.TrimSpace(q.Get("size")))
	ws, hs, ok := strings.Cut(size, "x")
	w, err1 := strconv.Atoi(ws)
	h, err2 := strconv.Atoi(hs)
	if !ok || err1 != nil || err2 != nil {
		return controlRequest{}, badRequest("size must look like 1280x720")
	}
	if w < 160 || w > 4096 || h < 120 || h > 3072 {
		return controlRequest{}, badRequest("size must be between 160x120 and 4096x3072")
	}
	return single(fmt.Sprintf("resolution %dx%d", w, h), cmdResolution, uint32(w)<<16|uint32(h), nil), nil
}

func buildKeyframeRequest(url.Values, *cameraState, time.Time) (controlRequest, error) {
	return single("key frame", cmdKeyframe, 1, nil), nil
}

// timePayload is the 32-byte clock payload (local time fields + unix time).
func timePayload(now time.Time) []byte {
	p := make([]byte, 32)
	vals := []uint32{
		uint32(now.Year()), uint32(now.Month()), uint32(now.Day()),
		uint32(now.Hour()), uint32(now.Minute()), uint32(now.Second()),
		uint32(now.Unix()), 0,
	}
	for i, v := range vals {
		binary.LittleEndian.PutUint32(p[4*i:], v)
	}
	return p
}

func buildTimeRequest(_ url.Values, _ *cameraState, now time.Time) (controlRequest, error) {
	return single("set clock "+now.Format("2006-01-02 15:04:05"), cmdTime, 2, timePayload(now)), nil
}

func buildAudioRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	on, err := parseOn(q)
	if err != nil {
		return controlRequest{}, err
	}
	sub := uint32(0)
	if on {
		sub = 1
	}
	return single("audio "+onOff(on), cmdAudio, sub, nil), nil
}

func buildRebootRequest(url.Values, *cameraState, time.Time) (controlRequest, error) {
	r := single("reboot", cmdReboot, 0, nil)
	r.repeat, r.gap = 3, 200*time.Millisecond
	return r, nil
}

func buildQueryRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	what := strings.ToLower(q.Get("what"))
	switch what {
	case "sleep":
		return single("query sleep", cmdSleepConfig, 0, nil), nil
	case "ir":
		return single("query ir", cmdNight, 1, nil), nil
	case "alarm":
		return single("query alarm", cmdAlarm, 1, nil), nil
	case "media":
		return single("query media", cmdMediaInfo, 0, nil), nil
	}
	return controlRequest{}, badRequest("what must be sleep, ir, alarm or media")
}

func parseBits(name, v string) (uint32, error) {
	if v == "" {
		return 0, nil
	}
	n, err := strconv.ParseUint(strings.TrimSpace(v), 0, 32)
	if err != nil {
		return 0, badRequest("%s must be a 32-bit number (decimal or 0x hex)", name)
	}
	return uint32(n), nil
}

// applySleepFlags returns a modified copy of an 88-byte sleep config.
func applySleepFlags(raw []byte, set, clear uint32) []byte {
	out := append([]byte(nil), raw...)
	old := binary.LittleEndian.Uint32(out[8:])
	binary.LittleEndian.PutUint32(out[8:], (old|set)&^clear)
	return out
}

func buildSleepFlagsRequest(q url.Values, st *cameraState, _ time.Time) (controlRequest, error) {
	if q.Get("set") == "" && q.Get("clear") == "" {
		return controlRequest{}, badRequest("give set=BITS and/or clear=BITS")
	}
	set, err := parseBits("set", q.Get("set"))
	if err != nil {
		return controlRequest{}, err
	}
	clear, err := parseBits("clear", q.Get("clear"))
	if err != nil {
		return controlRequest{}, err
	}
	raw := st.sleepRawCopy()
	if raw == nil {
		return controlRequest{}, &apiError{status: http.StatusConflict,
			msg: "no sleep config read yet; call /control/query?what=sleep first"}
	}
	cfg := applySleepFlags(raw, set, clear)
	label := fmt.Sprintf("sleep flags 0x%x", binary.LittleEndian.Uint32(cfg[8:]))
	return single(label, cmdSleepConfig, 1, cfg), nil
}

func buildSleepRawRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	h := strings.TrimSpace(q.Get("hex"))
	if len(h) != 2*sleepConfigLen {
		return controlRequest{}, badRequest("hex must be exactly %d hex characters", 2*sleepConfigLen)
	}
	b, err := hex.DecodeString(h)
	if err != nil {
		return controlRequest{}, badRequest("hex is not valid hexadecimal")
	}
	return single("sleep config (raw)", cmdSleepConfig, 1, b), nil
}

func buildProbeRequest(q url.Values, _ *cameraState, _ time.Time) (controlRequest, error) {
	cmd, err := strconv.ParseUint(strings.TrimSpace(q.Get("cmd")), 0, 32)
	if err != nil {
		return controlRequest{}, badRequest("cmd must be a number")
	}
	sub, err := strconv.ParseUint(strings.TrimSpace(q.Get("sub")), 0, 32)
	if err != nil {
		return controlRequest{}, badRequest("sub must be 0 or 1")
	}
	if cmd > 0xff {
		return controlRequest{}, badRequest("cmd must be at most 0xff")
	}
	if sub > 1 {
		return controlRequest{}, badRequest("sub must be 0 or 1")
	}
	switch cmd {
	case cmdOpenOrStream, cmdClose, cmdResolution, cmdBrightness, cmdContrast, cmdTurn, cmdReboot:
		return controlRequest{}, badRequest("cmd 0x%x changes the camera; it is not allowed as a probe", cmd)
	}
	return single(fmt.Sprintf("probe cmd 0x%x sub %d", cmd, sub), uint32(cmd), uint32(sub), nil), nil
}
