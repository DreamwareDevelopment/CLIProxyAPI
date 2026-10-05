package management

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
	coreauth "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/auth"
	cliproxyexecutor "github.com/router-for-me/CLIProxyAPI/v8/sdk/cliproxy/executor"
)

func TestCombinedQuotaRecoveryAndRouting(t *testing.T) {
	for _, tc := range []struct {
		name                            string
		exhausted, newFailure, redirect bool
	}{
		{name: "capacity"}, {name: "exhausted", exhausted: true}, {name: "newer failure", newFailure: true}, {name: "redirect", redirect: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			manager := coreauth.NewManager(nil, &coreauth.GreedySelector{}, nil)
			now := time.Now()
			deadline := now.Add(time.Hour)
			weekly := now.Add(24 * time.Hour).UTC().Truncate(time.Second)
			five := now.Add(2 * time.Hour).UTC().Truncate(time.Second)
			auth, err := manager.Register(context.Background(), &coreauth.Auth{ID: "z-usage", Provider: "claude", Status: coreauth.StatusError, Unavailable: true, Attributes: map[string]string{"runtime_only": "true"}, Metadata: map[string]any{"access_token": "selected-token"}, LastError: &coreauth.Error{HTTPStatus: 429, Message: "quota exceeded"}, Quota: coreauth.QuotaState{Exceeded: true, Reason: "quota", NextRecoverAt: deadline}, NextRetryAfter: deadline})
			if err != nil {
				t.Fatal(err)
			}
			utilization := 20
			if tc.exhausted {
				utilization = 100
			}
			payload, _ := json.Marshal(map[string]any{"seven_day": map[string]any{"utilization": utilization, "resets_at": weekly.Format(time.RFC3339)}, "five_hour": map[string]any{"utilization": 10, "resets_at": five.Format(time.RFC3339)}})
			calls := 0
			upstream := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				calls++
				if tc.newFailure {
					current, _ := manager.GetByID(auth.ID)
					current.LastError = &coreauth.Error{HTTPStatus: 403, Message: "newer forbidden"}
					if _, errUpdate := manager.Update(context.Background(), current); errUpdate != nil {
						t.Error(errUpdate)
					}
				}
				if tc.redirect && calls == 1 {
					http.Redirect(w, r, "/redirected", 302)
					return
				}
				_, _ = w.Write(payload)
			}))
			defer upstream.Close()
			old := http.DefaultTransport
			transport := &http.Transport{TLSClientConfig: &tls.Config{InsecureSkipVerify: true}, DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
				return (&net.Dialer{}).DialContext(ctx, network, upstream.Listener.Addr().String())
			}}
			http.DefaultTransport = transport
			defer func() { http.DefaultTransport = old; transport.CloseIdleConnections() }()
			body, _ := json.Marshal(map[string]any{"authIndex": auth.EnsureIndex(), "method": "GET", "url": "https://api.anthropic.com/api/oauth/usage", "header": map[string]string{"Authorization": "Bearer $TOKEN$"}})
			h := &Handler{authManager: manager}
			router := gin.New()
			router.POST("/", h.APICallV8)
			req := httptest.NewRequest("POST", "/", strings.NewReader(string(body)))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()
			router.ServeHTTP(rec, req)
			if rec.Code != 200 {
				t.Fatalf("status%d", rec.Code)
			}
			var response apiCallResponse
			if err := json.Unmarshal(rec.Body.Bytes(), &response); err != nil {
				t.Fatal(err)
			}
			if response.StatusCode != 200 || response.Body != string(payload) {
				t.Fatal("payload changed")
			}
			current, _ := manager.GetByID(auth.ID)
			blocked := tc.exhausted || tc.newFailure || tc.redirect
			if current.Quota.Exceeded != blocked || current.Unavailable != blocked {
				t.Fatalf("incorrect recovery: blocked=%v state=%+v", blocked, current)
			}
			if tc.newFailure || tc.redirect {
				if !current.QuotaResetSchedule.ObservedAt.IsZero() {
					t.Fatal("unproven schedule recorded")
				}
				if tc.newFailure && current.LastError.HTTPStatus != 403 {
					t.Fatal("newer failure lost")
				}
			} else {
				if !current.QuotaResetSchedule.WeeklyResetAt.Equal(weekly) || !current.QuotaResetSchedule.FiveHourResetAt.Equal(five) {
					t.Fatalf("schedule missing after recovery: %+v", current.QuotaResetSchedule)
				}
				if !tc.exhausted {
					other := &coreauth.Auth{ID: "a-later", Provider: "claude", QuotaResetSchedule: coreauth.QuotaResetSchedule{WeeklyResetAt: weekly.Add(time.Hour), ObservedAt: now}}
					picked, errPick := (&coreauth.GreedySelector{}).Pick(context.Background(), "claude", "", cliproxyexecutor.Options{}, []*coreauth.Auth{other, current})
					if errPick != nil || picked.ID != auth.ID {
						t.Fatalf("greedy ignored recovered schedule: %v %v", picked, errPick)
					}
				}
			}
		})
	}
}
