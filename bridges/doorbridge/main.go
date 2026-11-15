// Command bigeye-doorbridge connects to one door/window camera on the local
// network without any cloud service and re-serves its video and audio as
// RTSP, MJPEG and WAV, with an HTTP control API. Written from SPEC.md.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"sync"
	"syscall"
	"time"
)

// outputs dispatches decoded media to every consumer. It lives for the
// whole process; only the camera session restarts.
type outputs struct {
	state *cameraState
	rtsp  *rtspOutput // nil when RTSP is disabled
	jpegs *jpegHub
	pcm   *pcmHub

	dumpMu sync.Mutex
	dump   *os.File

	unknownLogged time.Time
}

func (o *outputs) videoFrame(kind frameKind, data []byte, now time.Time) {
	switch kind {
	case frameJPEG:
		o.state.frameReceived(now)
		o.jpegs.publish(data)
	case frameH264:
		o.state.frameReceived(now)
		o.writeDump(data)
		if o.rtsp != nil {
			o.rtsp.writeVideo(data, now)
		}
	default:
		if now.Sub(o.unknownLogged) > 10*time.Second {
			o.unknownLogged = now
			log.Printf("received a complete frame that is neither JPEG nor H.264; dropped")
		}
	}
}

func (o *outputs) audioSamples(samples []int16, now time.Time) {
	o.pcm.publish(samples)
	if o.rtsp != nil {
		o.rtsp.writeAudio(samples, now)
	}
}

func (o *outputs) writeDump(data []byte) {
	o.dumpMu.Lock()
	defer o.dumpMu.Unlock()
	if o.dump == nil {
		return
	}
	if _, err := o.dump.Write(data); err != nil {
		log.Printf("dump: write failed, stopping the dump: %v", err)
		o.dump.Close()
		o.dump = nil
	}
}

func parseIPv4(name, v string) (net.IP, error) {
	ip := net.ParseIP(v).To4()
	if ip == nil {
		return nil, fmt.Errorf("-%s must be an IPv4 address, got %q", name, v)
	}
	return ip, nil
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	ipFlag := flag.String("ip", "", "camera IPv4 address (required)")
	tokenFlag := flag.String("password-hash", "", "the camera's 12-character LAN token (required)")
	uidFlag := flag.String("uid", "", "camera UID (only logged)")
	cloudFlag := flag.String("cloud", "", "accepted for compatibility; must be empty (no cloud is ever contacted)")
	streamPort := flag.Int("stream-port", 0, "camera media UDP port; 0 = discover on every connect")
	localPort := flag.Int("local-port", 36891, "local UDP port for the session (random if busy)")
	rtspPort := flag.Int("rtsp-port", 8554, "RTSP listen port; 0 disables RTSP")
	httpAddr := flag.String("http", ":8765", "HTTP listen address; empty disables HTTP")
	mediaTimeout := flag.Int("media-timeout", 15, "seconds without a complete video frame before reconnecting")
	streamMode := flag.Int("stream-mode", 3, "value sent in the start-stream request (3 = live view)")
	advertise := flag.String("advertise-ip", "", "IPv4 to put into the hello instead of the auto-detected address")
	dumpPath := flag.String("dump", "", "append the raw H.264 Annex-B stream to this file (debugging)")
	flag.Parse()

	fail := func(format string, args ...any) {
		fmt.Fprintf(os.Stderr, format+"\n", args...)
		os.Exit(2)
	}
	if *ipFlag == "" {
		fail("-ip is required")
	}
	camIP, err := parseIPv4("ip", *ipFlag)
	if err != nil {
		fail("%v", err)
	}
	if len(*tokenFlag) != tokenLength {
		fail("-password-hash must be the camera's 12-character LAN token")
	}
	if *cloudFlag != "" {
		fail("-cloud must be empty: this bridge never contacts a cloud service")
	}
	if *mediaTimeout < 1 {
		fail("-media-timeout must be at least 1 second")
	}
	if *streamPort < 0 || *streamPort > 65535 || *localPort < 0 || *localPort > 65535 || *rtspPort < 0 || *rtspPort > 65535 {
		fail("port numbers must be between 0 and 65535")
	}
	if *streamMode < 0 {
		fail("-stream-mode must not be negative")
	}
	var advIP net.IP
	if *advertise != "" {
		if advIP, err = parseIPv4("advertise-ip", *advertise); err != nil {
			fail("%v", err)
		}
	}

	cfg := config{
		cameraIP:     camIP,
		token:        *tokenFlag,
		uid:          *uidFlag,
		streamPort:   *streamPort,
		localPort:    *localPort,
		mediaTimeout: time.Duration(*mediaTimeout) * time.Second,
		streamMode:   uint32(*streamMode),
		advertiseIP:  advIP,
	}
	log.Printf("bigeye-doorbridge starting for camera %s (uid %q)", camIP, cfg.uid)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	state := newCameraState()
	out := &outputs{state: state, jpegs: newJPEGHub(), pcm: newPCMHub()}
	if *dumpPath != "" {
		f, err := os.OpenFile(*dumpPath, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			log.Fatalf("cannot open dump file: %v", err)
		}
		out.dump = f
		defer f.Close()
	}

	if *rtspPort != 0 {
		r, err := newRTSPOutput(*rtspPort)
		if err != nil {
			log.Fatalf("%v", err)
		}
		out.rtsp = r
		defer r.close()
		log.Printf("RTSP server listening on rtsp://0.0.0.0:%d/cam", *rtspPort)
	}

	controls := make(chan controlRequest, controlQueueSize)
	if *httpAddr != "" {
		api := &apiServer{state: state, jpegs: out.jpegs, pcm: out.pcm, controls: controls, now: time.Now}
		srv := &http.Server{Addr: *httpAddr, Handler: api.routes(), ReadHeaderTimeout: 10 * time.Second}
		ln, err := net.Listen("tcp", *httpAddr)
		if err != nil {
			log.Fatalf("cannot listen for HTTP on %s: %v", *httpAddr, err)
		}
		go func() {
			if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
				log.Printf("HTTP server stopped: %v", err)
			}
		}()
		defer srv.Close()
		log.Printf("HTTP server listening on %s", *httpAddr)
	}

	runForever(ctx, cfg, state, out, controls)
	log.Printf("bigeye-doorbridge stopped")
}

// runForever keeps a camera session running, reconnecting with back-off.
func runForever(ctx context.Context, cfg config, state *cameraState, sink mediaSink, controls <-chan controlRequest) {
	bo := newBackoff()
	for {
		started := time.Now()
		err := runSession(ctx, cfg, state, sink, controls)
		if ctx.Err() != nil {
			log.Printf("session ended: shutting down")
			return
		}
		lasted := time.Since(started)
		wait := bo.delayAfter(lasted)
		log.Printf("session ended after %.0f s: %v; reconnecting in %.0f s", lasted.Seconds(), err, wait.Seconds())
		select {
		case <-ctx.Done():
			return
		case <-time.After(wait):
		}
	}
}
