package main

// HTTP server: MJPEG stream, snapshot, streaming WAV audio, status and the
// control API (SPEC.md sections 8-11).

import (
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"sync"
	"time"
)

const (
	mjpegBoundary   = "bigeyeframe"
	wavSilenceAfter = 300 * time.Millisecond
	wavSilenceLen   = audioSampleRate / 5 // 200 ms
)

// jpegHub keeps the newest JPEG and fans frames out to MJPEG clients.
// A client that is behind loses frames; it never blocks the others.
type jpegHub struct {
	mu     sync.Mutex
	latest []byte
	subs   map[chan []byte]struct{}
}

func newJPEGHub() *jpegHub { return &jpegHub{subs: make(map[chan []byte]struct{})} }

func (h *jpegHub) publish(jpeg []byte) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.latest = jpeg
	for ch := range h.subs {
		select {
		case ch <- jpeg:
		default: // client is behind: drop this frame for it
		}
	}
}

func (h *jpegHub) newest() []byte {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.latest
}

// subscribe returns a channel that starts with the newest frame (if any).
func (h *jpegHub) subscribe() chan []byte {
	ch := make(chan []byte, 2)
	h.mu.Lock()
	if h.latest != nil {
		ch <- h.latest
	}
	h.subs[ch] = struct{}{}
	h.mu.Unlock()
	return ch
}

func (h *jpegHub) unsubscribe(ch chan []byte) {
	h.mu.Lock()
	delete(h.subs, ch)
	h.mu.Unlock()
}

// apiServer bundles what the HTTP handlers need.
type apiServer struct {
	state    *cameraState
	jpegs    *jpegHub
	pcm      *pcmHub
	controls chan controlRequest
	now      func() time.Time
}

func (a *apiServer) routes() *http.ServeMux {
	mux := http.NewServeMux()
	mux.HandleFunc("/", a.handleIndex)
	mux.HandleFunc("/cam.mjpg", a.handleMJPEG)
	mux.HandleFunc("/snapshot.jpg", a.handleSnapshot)
	mux.HandleFunc("/audio.wav", a.handleWAV)
	mux.HandleFunc("/status", a.handleStatus)
	mux.HandleFunc("/control/state", a.handleControlState)
	for _, r := range controlRoutes() {
		mux.HandleFunc(r.path, a.controlHandler(r))
	}
	return mux
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	_ = enc.Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

const indexHTML = `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>bigeye-doorbridge</title>
<style>body{font-family:sans-serif;margin:2em;max-width:50em}code{background:#eee;padding:0 .2em}</style>
</head><body>
<h1>bigeye-doorbridge</h1>
<h2>Media</h2>
<ul>
<li><a href="/cam.mjpg">/cam.mjpg</a> - MJPEG stream (JPEG cameras)</li>
<li><a href="/snapshot.jpg">/snapshot.jpg</a> - newest JPEG frame</li>
<li><a href="/audio.wav">/audio.wav</a> - live audio (WAV, 8 kHz mono)</li>
<li>RTSP: <code>rtsp://&lt;this host&gt;:&lt;rtsp-port&gt;/cam</code> (H.264 cameras)</li>
</ul>
<h2>Status</h2>
<ul>
<li><a href="/status">/status</a></li>
<li><a href="/control/state">/control/state</a></li>
</ul>
<h2>Controls</h2>
<ul>
<li><a href="/ir?on=1">/ir?on=1</a> / <a href="/ir?on=0">/ir?on=0</a> - night vision</li>
<li><a href="/pir?on=1">/pir?on=1</a> / <a href="/pir?on=0">/pir?on=0</a> - motion detection</li>
<li><a href="/control/light?on=1">/control/light?on=1</a> / <a href="/control/light?on=0">/control/light?on=0</a></li>
<li><code>/control/picture?brightness=N</code>, <code>?contrast=N</code>, <code>?turn=flip|mirror</code></li>
<li><code>/control/resolution?size=WxH</code></li>
<li><a href="/control/audio?on=1">/control/audio?on=1</a> / <a href="/control/audio?on=0">/control/audio?on=0</a></li>
<li><a href="/control/query?what=sleep">query sleep</a>, <a href="/control/query?what=ir">query ir</a>,
<a href="/control/query?what=alarm">query alarm</a>, <a href="/control/query?what=media">query media</a></li>
<li>POST only: <code>/control/keyframe</code>, <code>/control/time</code>, <code>/reboot</code>,
<code>/control/sleepflags?set=BITS&amp;clear=BITS</code>, <code>/control/sleepraw?hex=...</code>,
<code>/control/probe?cmd=N&amp;sub=0|1</code></li>
</ul>
</body></html>
`

func (a *apiServer) handleIndex(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/" {
		http.NotFound(w, r)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(indexHTML))
}

func (a *apiServer) handleSnapshot(w http.ResponseWriter, r *http.Request) {
	jpeg := a.jpegs.newest()
	if jpeg == nil {
		http.Error(w, "no JPEG frame received yet", http.StatusNotFound)
		return
	}
	w.Header().Set("Content-Type", "image/jpeg")
	w.Header().Set("Content-Length", strconv.Itoa(len(jpeg)))
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(jpeg)
}

func (a *apiServer) handleMJPEG(w http.ResponseWriter, r *http.Request) {
	flusher, _ := w.(http.Flusher)
	ch := a.jpegs.subscribe()
	defer a.jpegs.unsubscribe(ch)

	w.Header().Set("Content-Type", "multipart/x-mixed-replace; boundary="+mjpegBoundary)
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	if flusher != nil {
		flusher.Flush()
	}
	for {
		select {
		case <-r.Context().Done():
			return
		case jpeg := <-ch:
			header := fmt.Sprintf("--%s\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n", mjpegBoundary, len(jpeg))
			if _, err := w.Write([]byte(header)); err != nil {
				return
			}
			if _, err := w.Write(jpeg); err != nil {
				return
			}
			if _, err := w.Write([]byte("\r\n")); err != nil {
				return
			}
			if flusher != nil {
				flusher.Flush()
			}
		}
	}
}

func (a *apiServer) handleWAV(w http.ResponseWriter, r *http.Request) {
	flusher, _ := w.(http.Flusher)
	ch := a.pcm.subscribe()
	defer a.pcm.unsubscribe(ch)

	w.Header().Set("Content-Type", "audio/wav")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	if _, err := w.Write(wavStreamHeader()); err != nil {
		return
	}
	if flusher != nil {
		flusher.Flush()
	}
	silence := make([]byte, 2*wavSilenceLen)
	timer := time.NewTimer(wavSilenceAfter)
	defer timer.Stop()
	for {
		var chunk []byte
		select {
		case <-r.Context().Done():
			return
		case samples := <-ch:
			chunk = pcmBytes(samples)
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
		case <-timer.C:
			chunk = silence
		}
		timer.Reset(wavSilenceAfter)
		if _, err := w.Write(chunk); err != nil {
			return
		}
		if flusher != nil {
			flusher.Flush()
		}
	}
}

func (a *apiServer) handleStatus(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.state.statusView(a.now()))
}

func (a *apiServer) handleControlState(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, a.state.controlView())
}

// enqueue puts a request into the control queue without blocking.
func (a *apiServer) enqueue(req controlRequest) bool {
	select {
	case a.controls <- req:
		return true
	default:
		return false
	}
}

func (a *apiServer) controlHandler(route controlRoute) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPost:
		case r.Method == http.MethodGet && !route.postOnly:
		default:
			allowed := "GET, POST"
			if route.postOnly {
				allowed = "POST"
			}
			w.Header().Set("Allow", allowed)
			writeError(w, http.StatusMethodNotAllowed, route.path+" accepts "+allowed+" only")
			return
		}
		if err := r.ParseForm(); err != nil {
			writeError(w, http.StatusBadRequest, "cannot parse parameters: "+err.Error())
			return
		}
		req, err := route.build(r.Form, a.state, a.now())
		if err != nil {
			var ae *apiError
			if errors.As(err, &ae) {
				writeError(w, ae.status, ae.msg)
			} else {
				writeError(w, http.StatusBadRequest, err.Error())
			}
			return
		}
		if !a.enqueue(req) {
			writeError(w, http.StatusServiceUnavailable, "control queue is full; try again shortly")
			return
		}
		log.Printf("control queued: %s", req.label)
		writeJSON(w, http.StatusOK, map[string]string{"queued": req.label})
	}
}
