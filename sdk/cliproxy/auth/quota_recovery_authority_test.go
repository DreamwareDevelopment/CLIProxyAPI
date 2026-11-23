package auth

import (
	"context"
	"net/http"
	"testing"
	"time"
)

func TestUsageResetRespectsRetainedProviderBlocks(t *testing.T) {
	_, snapshot, _, model := quotaGuardFixture(t)
	until := time.Now().Add(30 * time.Minute)
	hold := &Error{HTTPStatus: http.StatusForbidden, Message: "organization permission required"}
	cases := map[string]func(*Auth){
		"credential restriction retained under quota": func(a *Auth) {
			a.nonAuthCredentialLastError = hold
			a.nonAuthCredentialRetryAfter = until
		},
		"enforced credential quota": func(a *Auth) {
			a.forceCooldownSeeded = true
			a.credQuotaForceUntil = until
		},
		"model has an independent restriction": func(a *Auth) {
			a.ModelStates[model].credentialQuotaShadow = &ModelState{
				Unavailable: true, LastError: hold, NextRetryAfter: until,
			}
		},
		"model quota records a forced deadline": func(a *Auth) {
			a.ModelStates[model].quotaForceCooldownUntil = until
		},
	}
	for name, apply := range cases {
		t.Run(name, func(t *testing.T) {
			held := snapshot.Clone()
			apply(held)
			if QuotaRecoveryCandidate(held) {
				t.Fatal("confirmed quota capacity cannot clear this independent provider restriction")
			}
		})
	}
}

type quotaSaveUpdate struct {
	Store
	update func(*Auth)
}

func (s quotaSaveUpdate) Save(_ context.Context, a *Auth) (string, error) {
	s.update(a)
	return "", nil
}

func TestUsageResetRechecksStoreChanges(t *testing.T) {
	for _, name := range []string{"new account", "new permission restriction", "storage annotation"} {
		t.Run(name, func(t *testing.T) {
			m, initial, _, model := quotaGuardFixture(t)
			m.mu.Lock()
			m.auths[initial.ID].Metadata = map[string]any{"account_id": "original"}
			m.mu.Unlock()
			observed, _ := m.GetByID(initial.ID)
			m.SetStore(quotaSaveUpdate{update: func(saved *Auth) {
				switch name {
				case "new account":
					saved.Metadata["account_id"] = "replacement"
				case "new permission restriction":
					saved.ModelStates[model].LastError = &Error{HTTPStatus: http.StatusForbidden}
				case "storage annotation":
					saved.Metadata["note"] = "stored"
				}
			}})
			updated, _, err := m.ResetQuotaIfUnchanged(context.Background(), observed)
			if err != nil {
				t.Fatal(err)
			}
			current, _ := m.GetByID(initial.ID)
			if name == "storage annotation" {
				if updated == nil || current.Unavailable || current.Metadata["note"] != "stored" {
					t.Fatal("ordinary save annotation prevented recovery")
				}
			} else if updated != nil || !current.Unavailable || !current.Quota.Exceeded {
				t.Fatal("recovery published after the inspected account or access restriction changed")
			}
		})
	}
}
