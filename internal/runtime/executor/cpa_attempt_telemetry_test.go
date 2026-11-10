package executor

// Coverage for the instrumentation this repository adds to the Claude stream
// executor. These tests exist because the upstream suite cannot see our hooks:
// before they were written, deleting the outcome assignment or breaking the
// first-content latch left every package green. Each test below is required to
// fail when its hook is mutated.

import (
	"context"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/config"
	"github.com/router-for-me/CLIProxyAPI/v8/internal/cpatelemetry"
	claudeauth "github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude"
	cliproxyauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
	sdktranslator "github.com/router-for-me/CLIProxyAPI/v8/sdk/translator"
)

// attemptSink collects the spans FinishAttempt emits.
type attemptSink struct {
	mu     sync.Mutex
	events []cpatelemetry.Event
}

func (s *attemptSink) Submit(e cpatelemetry.Event) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.events = append(s.events, e)
}

func (s *attemptSink) attempts() []cpatelemetry.Event {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]cpatelemetry.Event, 0, len(s.events))
	for _, e := range s.events {
		if e.Kind == "attempt" {
			out = append(out, e)
		}
	}
	return out
}

// errAfter yields payload then fails, so the stream scanner reports an error
// without the upstream ever completing.
type errAfter struct {
	payload io.Reader
	err     error
}

func (r *errAfter) Read(p []byte) (int, error) {
	n, err := r.payload.Read(p)
	if errors.Is(err, io.EOF) {
		return n, r.err
	}
	return n, err
}

type fnRoundTripper func(*http.Request) (*http.Response, error)

func (f fnRoundTripper) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// runClaudeStream drives the real ingress middleware so the executor runs under
// a genuinely instrumented context, then fully drains the stream.
func runClaudeStream(t *testing.T, format sdktranslator.Format, body func() io.ReadCloser) *attemptSink {
	t.Helper()
	gin.SetMode(gin.TestMode)

	// The middleware carries its own sink on the request state, so the span
	// reaches this sink without touching global telemetry state.
	sink := &attemptSink{}
	obs := cpatelemetry.NewTelemetry(sink)

	transport := fnRoundTripper(func(req *http.Request) (*http.Response, error) {
		h := http.Header{}
		h.Set("Content-Type", "text/event-stream")
		return &http.Response{StatusCode: http.StatusOK, Header: h, Body: body(), Request: req}, nil
	})

	router := gin.New()
	router.Use(obs.Middleware())
	router.POST("/v1/messages", func(c *gin.Context) {
		ctx := context.WithValue(c.Request.Context(), "cliproxy.roundtripper", http.RoundTripper(transport))
		auth := &cliproxyauth.Auth{
			ID:         "test-attempt-telemetry",
			Attributes: map[string]string{"api_key": "sk-ant-oat-test-attempt-telemetry"},
			Metadata: map[string]any{
				"account_uuid": "11111111-2222-4333-8444-555555555555",
				claudeauth.ClaudeDeviceIDsMetadataKey: []string{
					"0000000000000000000000000000000000000000000000000000000000000001",
				},
			},
		}
		exec := NewClaudeExecutor(&config.Config{})
		payload := []byte(`{"model":"claude-opus-5","messages":[{"role":"user","content":"hi"}],"max_tokens":16}`)
		resp, err := exec.ExecuteStream(ctx, auth,
			cliproxyexecutor.Request{Model: "claude-opus-5", Payload: payload},
			cliproxyexecutor.Options{SourceFormat: format, Stream: true})
		if err != nil {
			t.Logf("ExecuteStream error: %v", err)
		}
		if err == nil && resp != nil && resp.Chunks != nil {
			for range resp.Chunks { // drain so the deferred FinishAttempt runs
				_ = 0
			}
		}
		c.Status(http.StatusOK)
	})

	req := httptest.NewRequest(http.MethodPost, "/v1/messages", strings.NewReader("{}"))
	router.ServeHTTP(httptest.NewRecorder(), req)

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if len(sink.attempts()) > 0 {
			break
		}
		time.Sleep(5 * time.Millisecond)
	}
	return sink
}

const textDeltaSSE = `event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}

event: message_stop
data: {"type":"message_stop"}

`

// A completed stream carrying a text delta must record first content, with the
// kind classified. Mutating the ObserveFirstContent hook or the
// firstContentSeen latch drops ContentKind and fails this test.
func TestClaudeStreamAttemptRecordsFirstContentKind(t *testing.T) {
	sink := runClaudeStream(t, sdktranslator.FormatClaude, func() io.ReadCloser {
		return io.NopCloser(strings.NewReader(textDeltaSSE))
	})

	attempts := sink.attempts()
	if len(attempts) == 0 {
		t.Fatal("no attempt span emitted; the stream executor was not instrumented")
	}
	a := attempts[0]
	if a.ContentKind != string(cpatelemetry.ContentText) {
		t.Fatalf("ContentKind = %q, want %q", a.ContentKind, cpatelemetry.ContentText)
	}
	if a.ContentElapsedMS == nil {
		t.Fatal("ContentElapsedMS not recorded, so first content was never observed")
	}
	if a.Outcome != string(cpatelemetry.OutcomeSuccess) {
		t.Fatalf("Outcome = %q, want %q", a.Outcome, cpatelemetry.OutcomeSuccess)
	}
	if a.Failed {
		t.Fatal("Failed set on a successful attempt")
	}
}

// A stream that dies mid-flight without completing must be recorded as a
// provider error. Mutating that outcome assignment makes the span report
// success and fails this test.
func TestClaudeStreamAttemptRecordsProviderErrorOnBrokenStream(t *testing.T) {
	sink := runClaudeStream(t, sdktranslator.FormatClaude, func() io.ReadCloser {
		return io.NopCloser(&errAfter{
			payload: strings.NewReader("event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"partial\"}}\n\n"),
			err:     errors.New("synthetic upstream stream failure"),
		})
	})

	attempts := sink.attempts()
	if len(attempts) == 0 {
		t.Fatal("no attempt span emitted; the stream executor was not instrumented")
	}
	a := attempts[0]
	if a.Outcome != string(cpatelemetry.OutcomeProviderError) {
		t.Fatalf("Outcome = %q, want %q on a broken stream", a.Outcome, cpatelemetry.OutcomeProviderError)
	}
	if !a.Failed {
		t.Fatal("Failed not set on a provider error")
	}
}

// The executor has two stream loops: a Claude passthrough loop and a
// translation loop for every other source format. Each carries its own
// provider-error outcome assignment, so the passthrough test above does not
// cover the translation one. Mutating that second assignment fails this test.
func TestClaudeStreamAttemptRecordsProviderErrorOnTranslatedStream(t *testing.T) {
	sink := runClaudeStream(t, sdktranslator.FormatOpenAI, func() io.ReadCloser {
		return io.NopCloser(&errAfter{
			payload: strings.NewReader("event: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"text_delta\",\"text\":\"partial\"}}\n\n"),
			err:     errors.New("synthetic upstream stream failure"),
		})
	})

	attempts := sink.attempts()
	if len(attempts) == 0 {
		t.Fatal("no attempt span emitted on the translation path")
	}
	a := attempts[0]
	if a.Outcome != string(cpatelemetry.OutcomeProviderError) {
		t.Fatalf("Outcome = %q, want %q on a broken translated stream", a.Outcome, cpatelemetry.OutcomeProviderError)
	}
	if !a.Failed {
		t.Fatal("Failed not set on a provider error")
	}
}
