package main

// Reconnect back-off (SPEC.md section 12).

import "time"

const (
	backoffBase       = 2 * time.Second
	backoffMax        = 60 * time.Second
	backoffLongEnough = 60 * time.Second
)

// backoff decides how long to wait before the next session attempt.
// Short sessions (under a minute, including failed attempts) double the
// delay up to the cap; a session longer than a minute resets it.
type backoff struct {
	base, max time.Duration
	next      time.Duration
}

func newBackoff() *backoff {
	return &backoff{base: backoffBase, max: backoffMax, next: backoffBase}
}

// delayAfter returns the wait after a session that lasted d.
func (b *backoff) delayAfter(d time.Duration) time.Duration {
	if d > backoffLongEnough {
		b.next = b.base
		return b.base
	}
	wait := b.next
	b.next *= 2
	if b.next > b.max {
		b.next = b.max
	}
	return wait
}
