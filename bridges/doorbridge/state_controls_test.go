package main

import (
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"
)

// mkControl builds a camera message of the given kind.
func mkControl(kind uint16, cmd, sub uint32, payload []byte) []byte {
	b := buildControl(1, cmd, sub, payload)
	binary.LittleEndian.PutUint16(b[4:], kind)
	return b
}

func mustMessage(t *testing.T, b []byte) controlMessage {
	t.Helper()
	m, ok := parseControlMessage(b)
	if !ok {
		t.Fatal("control message not parsed")
	}
	return m
}

func TestStatusReport(t *testing.T) {
	b := mkControl(kindControl, 13, 9, make([]byte, 20)) // 48 bytes in total
	binary.LittleEndian.PutUint32(b[40:], 0x100|87)
	binary.LittleEndian.PutUint32(b[44:], 4012)
	r, ok := parseStatusReport(mustMessage(t, b))
	if !ok || r.batteryPct != 87 || !r.charging || r.batteryMV != 4012 {
		t.Fatalf("status %+v ok=%v", r, ok)
	}
	if _, ok := parseStatusReport(mustMessage(t, mkControl(kindControl, 13, 9, make([]byte, 21)))); ok {
		t.Fatal("49-byte packet accepted as status report")
	}

	st := newCameraState()
	now := time.Unix(2000, 0)
	if v := st.statusView(now); v.StatusSeen || v.StatusAge != -1 || v.FrameAge != -1 {
		t.Fatalf("empty status %+v", v)
	}
	st.recordMessage(mustMessage(t, b), now)
	st.frameReceived(now.Add(-1500 * time.Millisecond))
	v := st.statusView(now.Add(2 * time.Second))
	if !v.StatusSeen || v.BatteryPct != 87 || v.StatusAge != 2 || !v.FrameSeen || v.FrameAge != 3.5 {
		t.Fatalf("status view %+v", v)
	}
}

func TestMediaInfo(t *testing.T) {
	p := make([]byte, 20)
	binary.LittleEndian.PutUint32(p[0:], 1)
	binary.LittleEndian.PutUint32(p[12:], 720<<16|1280)
	binary.LittleEndian.PutUint32(p[16:], 2)
	mi, ok := parseMediaInfo(mustMessage(t, mkControl(kindReply, 0x0f, 0, p)))
	if !ok || mi.Codec != "mjpeg" || mi.Width != 1280 || mi.Height != 720 || mi.Profile != 2 {
		t.Fatalf("media info %+v", mi)
	}
	if _, ok := parseMediaInfo(mustMessage(t, mkControl(kindReply, 0x0f, 0, p[:19]))); ok {
		t.Fatal("short media info accepted")
	}
}

func sampleSleepConfig() []byte {
	c := make([]byte, sleepConfigLen)
	c[0], c[1], c[2], c[3] = 3, 1, 64, 2
	binary.LittleEndian.PutUint16(c[4:], 3900)
	binary.LittleEndian.PutUint16(c[6:], 30)
	binary.LittleEndian.PutUint32(c[8:], 0b0101)
	binary.LittleEndian.PutUint32(c[12:], 600)
	binary.LittleEndian.PutUint32(c[16:], 7)
	binary.LittleEndian.PutUint32(c[20:], 8)
	return c
}

func TestSleepConfigAndFlags(t *testing.T) {
	raw := sampleSleepConfig()
	m := mustMessage(t, mkControl(kindReply, 0x22, 0, raw))
	sc, gotRaw, ok := parseSleepConfig(m)
	if !ok {
		t.Fatal("not parsed")
	}
	if sc.Version != 3 || sc.LastWakeup != 1 || sc.BatteryPct != 64 || sc.PIRLevel != 2 || sc.BatteryMV != 3900 ||
		sc.IdleWakeTime != 30 || sc.Flags != 5 || !sc.SleepEnabled || sc.WakeFromLAN || !sc.SleepWhenIdle ||
		sc.NoSleepOnCharger || sc.WakeupTime != 600 || sc.WakeupFunc1 != 7 || sc.WakeupFunc2 != 8 {
		t.Fatalf("sleep config %+v", sc)
	}
	if hex.EncodeToString(gotRaw) != hex.EncodeToString(raw) {
		t.Fatal("raw bytes differ")
	}

	st := newCameraState()
	q := url.Values{"set": {"0x2"}, "clear": {"1"}}
	if _, err := buildSleepFlagsRequest(q, st, time.Now()); err == nil || err.(*apiError).status != 409 {
		t.Fatalf("expected 409 without a config, got %v", err)
	}
	st.recordMessage(m, time.Unix(3000, 0))
	req, err := buildSleepFlagsRequest(q, st, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	cfg := req.steps[0].payload
	if req.steps[0].cmd != 0x22 || req.steps[0].sub != 1 || len(cfg) != 88 {
		t.Fatalf("request %+v", req.steps[0])
	}
	if got := binary.LittleEndian.Uint32(cfg[8:]); got != 0b0110 {
		t.Fatalf("flags = %b, want 110", got)
	}
	if cfg[2] != 64 || binary.LittleEndian.Uint32(cfg[12:]) != 600 {
		t.Fatal("other config bytes changed")
	}

	view := st.controlView()
	if view.SleepConfig == nil || view.SleepConfig.ReceivedUnix != 3000 || view.SleepRaw != hex.EncodeToString(raw) {
		t.Fatalf("control view %+v", view)
	}
	if _, ok := view.LastByCmd["34"]; !ok {
		t.Fatalf("last_by_cmd keys %v", view.LastByCmd)
	}
}

func TestNightVisionAndMessageLine(t *testing.T) {
	p := make([]byte, 12)
	p[8] = 1
	b := mkControl(kindReply, 0x13, 1, p)
	m := mustMessage(t, b)
	st := newCameraState()
	if st.controlView().NightVision != nil {
		t.Fatal("night vision should be null before a reply")
	}
	st.recordMessage(m, time.Now())
	if nv := st.controlView().NightVision; nv == nil || !*nv {
		t.Fatal("night vision not exposed")
	}
	line := messageLine(m)
	want := "type2 sub1 " + hex.EncodeToString(b[12:])
	if line != want {
		t.Fatalf("line %q, want %q", line, want)
	}
	js, _ := json.Marshal(st.controlView())
	if !strings.Contains(string(js), `"19":"type2 sub1 13000000`) {
		t.Fatalf("json %s", js)
	}
}

func TestControlBuilders(t *testing.T) {
	now := time.Date(2026, 11, 16, 14, 5, 9, 0, time.Local)
	st := newCameraState()
	check := func(name string, b func(url.Values, *cameraState, time.Time) (controlRequest, error), q url.Values,
		cmd, sub uint32, payloadHex string, repeat int) {
		t.Helper()
		r, err := b(q, st, now)
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		s := r.steps[0]
		if s.cmd != cmd || s.sub != sub || hex.EncodeToString(s.payload) != payloadHex || r.repeat != repeat {
			t.Fatalf("%s: got cmd %#x sub %d payload %x repeat %d", name, s.cmd, s.sub, s.payload, r.repeat)
		}
	}
	check("ir on", buildIRRequest, url.Values{"on": {"1"}}, 0x13, 3, "", 3)
	check("ir off", buildIRRequest, url.Values{"on": {"off"}}, 0x13, 4, "", 3)
	check("pir on", buildPIRRequest, url.Values{"on": {"true"}}, 0x0d, 10, "010000000000000000000000", 1)
	check("light on", buildLightRequest, url.Values{"on": {"1"}}, 0x13, 5, "ff000100ff00000000000000", 2)
	check("light off", buildLightRequest, url.Values{"on": {"0"}}, 0x13, 5, "ff000000ff00000000000000", 2)
	check("brightness", buildPictureRequest, url.Values{"brightness": {"40"}}, 6, 40, "", 1)
	check("contrast", buildPictureRequest, url.Values{"contrast": {"100"}}, 7, 100, "", 1)
	check("mirror", buildPictureRequest, url.Values{"turn": {"mirror"}}, 8, 2, "", 1)
	check("resolution", buildResolutionRequest, url.Values{"size": {"1280x720"}}, 4, 1280<<16|720, "", 1)
	check("keyframe", buildKeyframeRequest, nil, 0x0c, 1, "", 1)
	check("audio on", buildAudioRequest, url.Values{"on": {"1"}}, 1, 1, "", 1)
	check("reboot", buildRebootRequest, nil, 0x50, 0, "", 3)
	check("query media", buildQueryRequest, url.Values{"what": {"media"}}, 0x0f, 0, "", 1)
	check("query sleep", buildQueryRequest, url.Values{"what": {"sleep"}}, 0x22, 0, "", 1)
	check("probe", buildProbeRequest, url.Values{"cmd": {"0x11"}, "sub": {"1"}}, 0x11, 1, "", 1)

	// Clock payload: year, month, day, hour, minute, second, unix, 0.
	r, _ := buildTimeRequest(nil, st, now)
	p := r.steps[0].payload
	vals := []uint32{2026, 11, 16, 14, 5, 9, uint32(now.Unix()), 0}
	if r.steps[0].cmd != 0x15 || r.steps[0].sub != 2 || len(p) != 32 {
		t.Fatalf("time request %+v", r.steps[0])
	}
	for i, v := range vals {
		if binary.LittleEndian.Uint32(p[4*i:]) != v {
			t.Fatalf("time field %d = %d, want %d", i, binary.LittleEndian.Uint32(p[4*i:]), v)
		}
	}

	// Parameter errors.
	bad := []struct {
		name string
		b    func(url.Values, *cameraState, time.Time) (controlRequest, error)
		q    url.Values
	}{
		{"ir missing", buildIRRequest, url.Values{}},
		{"ir junk", buildIRRequest, url.Values{"on": {"maybe"}}},
		{"brightness 101", buildPictureRequest, url.Values{"brightness": {"101"}}},
		{"picture empty", buildPictureRequest, url.Values{}},
		{"turn junk", buildPictureRequest, url.Values{"turn": {"spin"}}},
		{"resolution small", buildResolutionRequest, url.Values{"size": {"100x100"}}},
		{"resolution junk", buildResolutionRequest, url.Values{"size": {"big"}}},
		{"query junk", buildQueryRequest, url.Values{"what": {"x"}}},
		{"sleepraw short", buildSleepRawRequest, url.Values{"hex": {"00"}}},
		{"probe reboot", buildProbeRequest, url.Values{"cmd": {"0x50"}, "sub": {"0"}}},
		{"probe stream", buildProbeRequest, url.Values{"cmd": {"0"}, "sub": {"0"}}},
		{"probe big cmd", buildProbeRequest, url.Values{"cmd": {"256"}, "sub": {"0"}}},
		{"probe sub 2", buildProbeRequest, url.Values{"cmd": {"0x11"}, "sub": {"2"}}},
	}
	for _, c := range bad {
		_, err := c.b(c.q, st, now)
		ae, ok := err.(*apiError)
		if !ok || ae.status != http.StatusBadRequest {
			t.Errorf("%s: expected 400, got %v", c.name, err)
		}
	}

	r, err := buildSleepRawRequest(url.Values{"hex": {strings.Repeat("ab", 88)}}, st, now)
	if err != nil || len(r.steps[0].payload) != 88 || r.steps[0].cmd != 0x22 || r.steps[0].sub != 1 {
		t.Fatalf("sleepraw: %v %+v", err, r)
	}
}

func TestControlHTTP(t *testing.T) {
	api := &apiServer{
		state:    newCameraState(),
		jpegs:    newJPEGHub(),
		pcm:      newPCMHub(),
		controls: make(chan controlRequest, controlQueueSize),
		now:      time.Now,
	}
	h := api.routes()
	do := func(method, target string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(method, target, nil))
		return rec
	}

	rec := do("GET", "/ir?on=1")
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), `"queued": "night vision on"`) {
		t.Fatalf("ir: %d %s", rec.Code, rec.Body)
	}
	if rec := do("GET", "/reboot"); rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("GET /reboot: %d", rec.Code)
	}
	if rec := do("GET", "/ir?on=x"); rec.Code != 400 {
		t.Fatalf("bad on: %d", rec.Code)
	}
	if rec := do("POST", "/control/sleepflags?set=1"); rec.Code != 409 {
		t.Fatalf("sleepflags without config: %d", rec.Code)
	}
	for i := 1; i < controlQueueSize; i++ {
		if rec := do("POST", "/control/keyframe"); rec.Code != 200 {
			t.Fatalf("queue entry %d: %d", i, rec.Code)
		}
	}
	if rec := do("POST", "/control/keyframe"); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("full queue: %d", rec.Code)
	}

	if rec := do("GET", "/snapshot.jpg"); rec.Code != 404 {
		t.Fatalf("snapshot without frame: %d", rec.Code)
	}
	api.jpegs.publish([]byte{0xFF, 0xD8, 0xFF, 0xD9})
	if rec := do("GET", "/snapshot.jpg"); rec.Code != 200 || rec.Body.Len() != 4 {
		t.Fatalf("snapshot: %d", rec.Code)
	}
	rec = do("GET", "/status")
	var st map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &st); err != nil || st["frame_age_s"] != -1.0 || st["status_seen"] != false {
		t.Fatalf("status: %s", rec.Body)
	}
	rec = do("GET", "/control/state")
	if !strings.Contains(rec.Body.String(), `"media": null`) || !strings.Contains(rec.Body.String(), `"night_vision": null`) {
		t.Fatalf("control state: %s", rec.Body)
	}
	if rec := do("GET", "/"); rec.Code != 200 || !strings.Contains(rec.Body.String(), "/cam.mjpg") {
		t.Fatalf("index: %d", rec.Code)
	}
}

func TestJPEGHubDropsForSlowClient(t *testing.T) {
	h := newJPEGHub()
	h.publish([]byte{1})
	ch := h.subscribe()
	if got := <-ch; got[0] != 1 {
		t.Fatal("subscriber did not start with the newest frame")
	}
	for i := 2; i < 10; i++ {
		h.publish([]byte{byte(i)}) // must never block
	}
	if len(ch) != cap(ch) {
		t.Fatalf("channel holds %d frames", len(ch))
	}
	if h.newest()[0] != 9 {
		t.Fatal("newest frame wrong")
	}
}

func TestBackoff(t *testing.T) {
	b := newBackoff()
	short := 5 * time.Second
	want := []time.Duration{2, 4, 8, 16, 32, 60, 60}
	for i, w := range want {
		if got := b.delayAfter(short); got != w*time.Second {
			t.Fatalf("attempt %d: %v, want %v", i, got, w*time.Second)
		}
	}
	if got := b.delayAfter(2 * time.Minute); got != 2*time.Second {
		t.Fatalf("after a long session: %v", got)
	}
	if got := b.delayAfter(short); got != 2*time.Second {
		t.Fatalf("first short after reset: %v", got)
	}
	if got := b.delayAfter(short); got != 4*time.Second {
		t.Fatalf("second short after reset: %v", got)
	}
}
