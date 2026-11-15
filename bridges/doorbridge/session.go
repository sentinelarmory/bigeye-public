package main

// One camera session: discovery, hello, bootstrap, the receive loop with
// acknowledgements, keepalives, controls, and the polite ending
// (SPEC.md sections 3-7, 11 and 12).

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net"
	"time"
)

// config holds the command-line settings that a session needs.
type config struct {
	cameraIP     net.IP
	token        string
	uid          string
	streamPort   int
	localPort    int
	mediaTimeout time.Duration
	streamMode   uint32
	advertiseIP  net.IP
}

// mediaSink receives what a session decodes. Implementations must not block.
type mediaSink interface {
	videoFrame(kind frameKind, data []byte, now time.Time)
	audioSamples(samples []int16, now time.Time)
}

const (
	discoveryTries   = 5
	discoveryWait    = 1200 * time.Millisecond
	helloTries       = 5
	helloInterval    = time.Second
	helloWindow      = 6 * time.Second
	bootstrapGap     = 20 * time.Millisecond
	keepaliveEvery   = 2 * time.Second
	keepaliveFPS     = 15
	ackTick          = 20 * time.Millisecond
	ackMaxQuiet      = 40 * time.Millisecond
	statusLogEvery   = 10 * time.Second
	minFirstFrame    = 6 * time.Second
	maxUDPPacketSize = 2048
)

// discoverMediaPort asks the camera for its media UDP port.
func discoverMediaPort(ctx context.Context, camera net.IP) (int, error) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{Port: discoveryPort})
	if err != nil {
		conn, err = net.ListenUDP("udp4", &net.UDPAddr{})
		if err != nil {
			return 0, fmt.Errorf("cannot open a discovery socket: %w", err)
		}
	}
	defer conn.Close()

	dst := &net.UDPAddr{IP: camera, Port: discoveryPort}
	req := buildDiscoveryRequest()
	buf := make([]byte, maxUDPPacketSize)
	for try := 1; try <= discoveryTries; try++ {
		if ctx.Err() != nil {
			return 0, ctx.Err()
		}
		if _, err := conn.WriteToUDP(req, dst); err != nil {
			return 0, fmt.Errorf("cannot send discovery request: %w", err)
		}
		deadline := time.Now().Add(discoveryWait)
		for time.Now().Before(deadline) {
			_ = conn.SetReadDeadline(deadline)
			n, from, err := conn.ReadFromUDP(buf)
			if err != nil {
				break
			}
			if !from.IP.Equal(camera) {
				continue
			}
			if port := parseDiscoveryReply(buf[:n], from.Port); port > 0 {
				log.Printf("discovery: camera answered (%d bytes), media port %d", n, port)
				return port, nil
			}
		}
		log.Printf("discovery: no answer from %s (try %d of %d)", camera, try, discoveryTries)
	}
	return 0, errors.New("camera did not answer discovery (is it powered and on Wi-Fi?)")
}

// openSessionSocket binds the preferred local port, or a random one.
func openSessionSocket(port int) (*net.UDPConn, error) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{Port: port})
	if err == nil {
		return conn, nil
	}
	log.Printf("local UDP port %d is not available (%v); using a random port", port, err)
	return net.ListenUDP("udp4", &net.UDPAddr{})
}

// localIPFor returns the local address of the interface that routes to dst.
// Connecting a UDP socket only consults the routing table; nothing is sent.
func localIPFor(dst *net.UDPAddr) (net.IP, error) {
	c, err := net.DialUDP("udp4", nil, dst)
	if err != nil {
		return nil, err
	}
	defer c.Close()
	return c.LocalAddr().(*net.UDPAddr).IP, nil
}

// sayHello sends the LAN hello and waits for the camera to accept it.
func sayHello(ctx context.Context, conn *net.UDPConn, dst *net.UDPAddr, hello []byte) (uint32, error) {
	buf := make([]byte, maxUDPPacketSize)
	start := time.Now()
	end := start.Add(helloWindow)
	sent := 0
	nextSend := start
	for time.Now().Before(end) {
		if ctx.Err() != nil {
			return 0, ctx.Err()
		}
		if sent < helloTries && !time.Now().Before(nextSend) {
			if _, err := conn.WriteToUDP(hello, dst); err != nil {
				return 0, fmt.Errorf("cannot send hello: %w", err)
			}
			sent++
			nextSend = nextSend.Add(helloInterval)
		}
		_ = conn.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
		n, from, err := conn.ReadFromUDP(buf)
		if err != nil {
			var ne net.Error
			if errors.As(err, &ne) && ne.Timeout() {
				continue
			}
			return 0, fmt.Errorf("socket error while waiting for the hello answer: %w", err)
		}
		if !from.IP.Equal(dst.IP) {
			continue
		}
		if id, ok := parseHelloReply(buf[:n]); ok {
			return id, nil
		}
	}
	return 0, errors.New("camera did not answer the hello: wrong LAN token? (it changes after a factory reset)")
}

// session is the state of one connected camera session.
type session struct {
	cfg   config
	state *cameraState
	sink  mediaSink
	conn  *net.UDPConn
	dst   *net.UDPAddr
	id    uint32
	asm   *assembler
}

func (s *session) send(b []byte) error {
	_, err := s.conn.WriteToUDP(b, s.dst)
	return err
}

func (s *session) sendControl(st controlStep) error {
	return s.send(buildControl(s.id, st.cmd, st.sub, st.payload))
}

// flushAcks sends all pending acknowledgements and due retransmission requests.
func (s *session) flushAcks(now time.Time) error {
	items := s.asm.collect(now)
	for _, pkt := range buildAcks(s.id, s.asm.fps(now), items) {
		if err := s.send(pkt); err != nil {
			return err
		}
	}
	return nil
}

// endPolitely tells the camera that this viewer is leaving.
func (s *session) endPolitely() {
	stop := controlStep{cmd: cmdOpenOrStream, sub: 1, payload: streamPayload(0)}
	_ = s.sendControl(stop)
	time.Sleep(15 * time.Millisecond)
	_ = s.sendControl(stop)
	time.Sleep(15 * time.Millisecond)
	closing := controlStep{cmd: cmdClose, sub: 1}
	_ = s.sendControl(closing)
	time.Sleep(10 * time.Millisecond)
	_ = s.sendControl(closing)
}

// runControls sends queued API controls until done is closed.
func (s *session) runControls(done <-chan struct{}, queue <-chan controlRequest) {
	for {
		var req controlRequest
		select {
		case <-done:
			return
		case req = <-queue:
		}
		repeat := req.repeat
		if repeat < 1 {
			repeat = 1
		}
		for _, st := range req.steps {
			for i := 0; i < repeat; i++ {
				if err := s.sendControl(st); err != nil {
					log.Printf("control %q: send failed: %v", req.label, err)
					return
				}
				if i < repeat-1 && req.gap > 0 {
					select {
					case <-done:
						return
					case <-time.After(req.gap):
					}
				}
			}
		}
		log.Printf("control sent: %s", req.label)
	}
}

type inbound struct {
	data []byte
	at   time.Time
}

// readLoop forwards packets from the camera until the socket is closed.
func (s *session) readLoop(done <-chan struct{}, out chan<- inbound, errc chan<- error) {
	buf := make([]byte, maxUDPPacketSize)
	for {
		n, from, err := s.conn.ReadFromUDP(buf)
		if err != nil {
			errc <- err
			return
		}
		if !from.IP.Equal(s.cfg.cameraIP) {
			continue
		}
		select {
		case out <- inbound{data: append([]byte(nil), buf[:n]...), at: time.Now()}:
		case <-done:
			return
		}
	}
}

// runSession runs one complete session and returns why it ended.
func runSession(ctx context.Context, cfg config, state *cameraState, sink mediaSink, queue <-chan controlRequest) error {
	port := cfg.streamPort
	if port == 0 {
		p, err := discoverMediaPort(ctx, cfg.cameraIP)
		if err != nil {
			return err
		}
		port = p
	}
	dst := &net.UDPAddr{IP: cfg.cameraIP, Port: port}

	conn, err := openSessionSocket(cfg.localPort)
	if err != nil {
		return fmt.Errorf("cannot open the session socket: %w", err)
	}
	defer conn.Close()
	localPort := conn.LocalAddr().(*net.UDPAddr).Port

	localIP := cfg.advertiseIP
	if localIP == nil {
		if localIP, err = localIPFor(dst); err != nil {
			return fmt.Errorf("cannot find a local address that routes to the camera: %w", err)
		}
	}
	hello, err := buildHello(localPort, localIP, cfg.token)
	if err != nil {
		return err
	}
	log.Printf("sending hello to %s from %s:%d", dst, localIP, localPort)
	id, err := sayHello(ctx, conn, dst, hello)
	if err != nil {
		return err
	}
	log.Printf("camera accepted the hello, session id %d", id)
	_ = conn.SetReadDeadline(time.Time{})

	s := &session{cfg: cfg, state: state, sink: sink, conn: conn, dst: dst, id: id, asm: newAssembler()}
	defer s.endPolitely()

	for i, st := range bootstrapSteps(cfg.streamMode) {
		if i > 0 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(bootstrapGap):
			}
		}
		if err := s.sendControl(st); err != nil {
			return fmt.Errorf("bootstrap failed: %w", err)
		}
	}
	log.Printf("stream requested (mode %d); waiting for video", cfg.streamMode)

	done := make(chan struct{})
	defer close(done)
	packets := make(chan inbound, 256)
	readErr := make(chan error, 1)
	go s.readLoop(done, packets, readErr)
	go s.runControls(done, queue)

	return s.loop(ctx, packets, readErr)
}

// loop is the receive loop of a running session.
func (s *session) loop(ctx context.Context, packets <-chan inbound, readErr <-chan error) error {
	ack := time.NewTicker(ackTick)
	defer ack.Stop()
	keep := time.NewTicker(keepaliveEvery)
	defer keep.Stop()
	status := time.NewTicker(statusLogEvery)
	defer status.Stop()

	started := time.Now()
	var lastFrame time.Time
	firstFrameLimit := s.cfg.mediaTimeout
	if firstFrameLimit < minFirstFrame {
		firstFrameLimit = minFirstFrame
	}
	mediaSeen := false

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()

		case err := <-readErr:
			return fmt.Errorf("session socket failed: %w", err)

		case in := <-packets:
			h, ok := parseSessionHeader(in.data)
			if !ok {
				continue
			}
			switch h.kind {
			case kindMedia:
				mp, err := parseMediaPacket(in.data)
				if err != nil {
					continue
				}
				if !mediaSeen {
					mediaSeen = true
					log.Printf("media is arriving from the camera")
				}
				ev := s.asm.push(mp, in.at)
				if ev.audio != nil {
					if samples := decodeIMABlock(ev.audio); samples != nil {
						s.sink.audioSamples(samples, in.at)
					}
				}
				if ev.frame != nil {
					if lastFrame.IsZero() {
						log.Printf("first complete video frame after %.1f s", in.at.Sub(started).Seconds())
					}
					lastFrame = in.at
					kind, data := classifyFrame(ev.frame)
					s.sink.videoFrame(kind, data, in.at)
				}
				if ev.sendNow {
					if err := s.flushAcks(in.at); err != nil {
						return fmt.Errorf("session socket failed: %w", err)
					}
				}
			case kindControl, kindReply:
				m, ok := parseControlMessage(in.data)
				if !ok {
					continue
				}
				s.state.recordMessage(m, in.at)
				switch m.cmd {
				case cmdClose:
					return errors.New("the camera closed the session")
				case cmdGoingToSleep:
					log.Printf("the camera announced that it is going to sleep")
				}
			}

		case now := <-ack.C:
			if lastFrame.IsZero() {
				if now.Sub(started) > firstFrameLimit {
					return fmt.Errorf("no complete video frame within %.0f s", firstFrameLimit.Seconds())
				}
			} else if now.Sub(lastFrame) > s.cfg.mediaTimeout {
				return fmt.Errorf("no complete video frame for %.0f s", s.cfg.mediaTimeout.Seconds())
			}
			if s.asm.hasPending() && now.Sub(s.asm.lastSend) >= ackMaxQuiet {
				if err := s.flushAcks(now); err != nil {
					return fmt.Errorf("session socket failed: %w", err)
				}
			}

		case <-keep.C:
			if err := s.sendControl(controlStep{cmd: cmdKeepalive, payload: keepalivePayload(keepaliveFPS)}); err != nil {
				return fmt.Errorf("session socket failed: %w", err)
			}

		case now := <-status.C:
			since := "never"
			if !lastFrame.IsZero() {
				since = fmt.Sprintf("%.1f s ago", now.Sub(lastFrame).Seconds())
			}
			log.Printf("status: last good frame %s, %d frames pending, %d abandoned so far",
				since, s.asm.pendingFrames(), s.asm.abandoned)
		}
	}
}
