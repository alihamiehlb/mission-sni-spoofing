// Package ratelimit implements per-IP token-bucket rate limiting.
// Thread-safe; uses sync.Map for lock-free read-heavy workloads.
package ratelimit

import (
	"log"
	"sync"
	"sync/atomic"
	"time"
)

// Limiter enforces per-IP request rate and bandwidth limits.
type Limiter struct {
	reqPerMin  float64
	bwPerSec   int64
	banThresh  int
	clients    sync.Map // clientIP → *clientState
	cleanupTkr *time.Ticker
}

type clientState struct {
	mu sync.Mutex

	// Token bucket
	tokens     float64
	lastRefill time.Time

	// Bandwidth sliding window (1s)
	bwBytes     int64
	bwWindowEnd time.Time

	// Auth failures
	authFails int
	bannedUntil time.Time
}

// New creates a Limiter.
//   - reqPerMin: maximum requests per minute per IP
//   - bwPerSec:  maximum bytes per second per IP (0 = unlimited)
//   - banThresh: ban after this many auth failures
func New(reqPerMin int, bwPerSec int64, banThresh int) *Limiter {
	l := &Limiter{
		reqPerMin: float64(reqPerMin),
		bwPerSec:  bwPerSec,
		banThresh: banThresh,
	}
	// Periodically clean stale entries
	l.cleanupTkr = time.NewTicker(5 * time.Minute)
	go l.cleanupLoop()
	return l
}

// Allow returns true if the IP is permitted to open a new connection.
func (l *Limiter) Allow(ip string) bool {
	st := l.getOrCreate(ip)
	st.mu.Lock()
	defer st.mu.Unlock()

	// Ban check
	if time.Now().Before(st.bannedUntil) {
		return false
	}

	// Initialise bucket if first use
	if st.tokens == 0 && st.lastRefill.IsZero() {
		st.tokens = l.reqPerMin // start full
		st.lastRefill = time.Now()
		st.tokens--
		return true
	}

	// Refill tokens based on elapsed time
	elapsed := time.Since(st.lastRefill).Seconds()
	refill := elapsed * (l.reqPerMin / 60.0)
	st.tokens = min(l.reqPerMin, st.tokens+refill)
	st.lastRefill = time.Now()

	if st.tokens < 1.0 {
		return false
	}
	st.tokens--
	return true
}

// AddBytes records bandwidth usage. Returns false if over limit.
func (l *Limiter) AddBytes(ip string, n int64) bool {
	if l.bwPerSec == 0 {
		return true
	}
	st := l.getOrCreate(ip)
	st.mu.Lock()
	defer st.mu.Unlock()

	now := time.Now()
	if now.After(st.bwWindowEnd) {
		// New 1-second window
		atomic.StoreInt64(&st.bwBytes, n)
		st.bwWindowEnd = now.Add(time.Second)
		return true
	}
	newTotal := atomic.AddInt64(&st.bwBytes, n)
	return newTotal <= l.bwPerSec
}

// RecordAuthFailure increments the failure counter and bans if threshold reached.
func (l *Limiter) RecordAuthFailure(ip string) {
	st := l.getOrCreate(ip)
	st.mu.Lock()
	defer st.mu.Unlock()
	st.authFails++
	if st.authFails >= l.banThresh {
		st.bannedUntil = time.Now().Add(5 * time.Minute)
		log.Printf("[ratelimit] banned %s (auth failures: %d)", ip, st.authFails)
	}
}

// ResetAuthFailures clears the failure counter after successful auth.
func (l *Limiter) ResetAuthFailures(ip string) {
	if v, ok := l.clients.Load(ip); ok {
		st := v.(*clientState)
		st.mu.Lock()
		st.authFails = 0
		st.mu.Unlock()
	}
}

func (l *Limiter) getOrCreate(ip string) *clientState {
	if v, ok := l.clients.Load(ip); ok {
		return v.(*clientState)
	}
	st := &clientState{}
	actual, _ := l.clients.LoadOrStore(ip, st)
	return actual.(*clientState)
}

func (l *Limiter) cleanupLoop() {
	for range l.cleanupTkr.C {
		now := time.Now()
		l.clients.Range(func(k, v interface{}) bool {
			st := v.(*clientState)
			st.mu.Lock()
			idle := now.After(st.bannedUntil) && st.authFails == 0
			st.mu.Unlock()
			if idle {
				l.clients.Delete(k)
			}
			return true
		})
	}
}

func min(a, b float64) float64 {
	if a < b {
		return a
	}
	return b
}
