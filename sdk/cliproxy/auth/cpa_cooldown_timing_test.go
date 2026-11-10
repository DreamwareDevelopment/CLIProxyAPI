package auth

import (
	"context"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/cpatelemetry"
)

type cooldownSink struct {
	mu     sync.Mutex
	events []cpatelemetry.Event
}

func (s *cooldownSink) Submit(e cpatelemetry.Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, e)
}

func (s *cooldownSink) waits() []cpatelemetry.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	var out []cpatelemetry.Event
	for _, e := range s.events {
		if e.Kind == "retry_wait" {
			out = append(out, e)
		}
	}
	return out
}

// instrumentedRequest runs fn inside a real instrumented request so the wait has
// a request root to hang from, exactly as a live credential selection does.
func instrumentedRequest(t *testing.T, fn func(ctx context.Context)) *cooldownSink {
	t.Helper()
	s := &cooldownSink{}
	router := gin.New()
	router.Use(cpatelemetry.NewTelemetry(s).Middleware())
	router.POST("/v1/messages", func(c *gin.Context) {
		fn(c.Request.Context())
		c.Status(200)
	})
	router.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/v1/messages", nil))
	return s
}

// The span must cover the wait that actually happened. jitteredCooldownWait adds
// a random increment on top of the requested duration, so a span reporting the
// requested figure would land exactly on it; a measured one cannot.
func TestWaitForCooldownSpansTheActualJitteredWait(t *testing.T) {
	// jitterRange is wait/4 here, so the real sleep lands in [200ms, 250ms).
	const wait = 200 * time.Millisecond
	const maxWait = 2 * time.Second
	var elapsed time.Duration
	s := instrumentedRequest(t, func(ctx context.Context) {
		start := time.Now()
		if err := waitForCooldown(ctx, wait, maxWait); err != nil {
			t.Errorf("waitForCooldown: %v", err)
		}
		elapsed = time.Since(start)
	})
	waits := s.waits()
	if len(waits) != 1 {
		t.Fatalf("retry_wait span count=%d, want 1", len(waits))
	}
	w := waits[0]
	measured := w.End.Sub(w.Start)
	if measured <= wait {
		t.Fatalf("measured wait %v does not exceed the requested %v: the jitter and the real sleep are not inside the span", measured, wait)
	}
	// The span is opened before the jitter is drawn and closed at the wake, so
	// it must account for essentially the whole call, not a slice of it.
	if measured > elapsed {
		t.Fatalf("measured wait %v exceeds the observed wall time %v", measured, elapsed)
	}
	if elapsed-measured > 15*time.Millisecond {
		t.Fatalf("span covers %v of an observed %v wait: %v of the real wait is outside it", measured, elapsed, elapsed-measured)
	}
	if w.RetryReason != "cooldown" {
		t.Fatalf("reason=%q", w.RetryReason)
	}
	if w.RetryCanceled == nil || *w.RetryCanceled {
		t.Fatalf("canceled=%v on a wait that ran to completion", w.RetryCanceled)
	}
	if w.Failed || w.Outcome != "success" {
		t.Fatalf("outcome=%q failed=%v", w.Outcome, w.Failed)
	}
}

func TestWaitForCooldownRecordsCancellation(t *testing.T) {
	s := instrumentedRequest(t, func(ctx context.Context) {
		canceled, cancel := context.WithCancel(ctx)
		cancel()
		if err := waitForCooldown(canceled, time.Hour, 2*time.Hour); err == nil {
			t.Error("a canceled wait must return its context error")
		}
	})
	waits := s.waits()
	if len(waits) != 1 {
		t.Fatalf("retry_wait span count=%d, want 1", len(waits))
	}
	w := waits[0]
	if w.RetryCanceled == nil || !*w.RetryCanceled {
		t.Fatalf("canceled=%v", w.RetryCanceled)
	}
	if !w.Failed || w.Outcome != "canceled" {
		t.Fatalf("outcome=%q failed=%v", w.Outcome, w.Failed)
	}
	if measured := w.End.Sub(w.Start); measured > time.Second {
		t.Fatalf("a cancellation waited %v", measured)
	}
}

// No wait happened, so there is no wait to report.
func TestWaitForCooldownWithoutAWaitEmitsNothing(t *testing.T) {
	s := instrumentedRequest(t, func(ctx context.Context) {
		if err := waitForCooldown(ctx, 0, time.Minute); err != nil {
			t.Errorf("waitForCooldown: %v", err)
		}
	})
	if waits := s.waits(); len(waits) != 0 {
		t.Fatalf("emitted %d wait spans for a zero wait", len(waits))
	}
}

// Selection outside an instrumented request must still work and record nothing.
func TestWaitForCooldownOutsideARequestIsInert(t *testing.T) {
	if err := waitForCooldown(context.Background(), time.Millisecond, time.Second); err != nil {
		t.Fatalf("waitForCooldown: %v", err)
	}
}
