package auth

import (
	"context"
	"net/http"
	"reflect"
	"strings"
	"time"

	internalconfig "github.com/router-for-me/CLIProxyAPI/v8/internal/config"
)

// ResetQuotaIfUnchanged recovers a local quota cooldown only while the inspected
// credential snapshot is still current. The caller must first confirm provider
// capacity for every affected quota window and model.
func (m *Manager) ResetQuotaIfUnchanged(ctx context.Context, expected *Auth) (*Auth, []string, error) {
	if expected == nil || strings.TrimSpace(expected.ID) == "" || strings.TrimSpace(expected.Provider) == "" {
		return nil, nil, nil
	}
	return m.resetQuota(ctx, expected.ID, expected)
}

// QuotaRecoveryCandidate reports whether all restrictions on a snapshot are
// attributable to quota. It neither confirms provider capacity nor mutates auth.
func QuotaRecoveryCandidate(auth *Auth) bool {
	if auth == nil || auth.Disabled || strings.TrimSpace(auth.ID) == "" || strings.TrimSpace(auth.Provider) == "" {
		return false
	}
	if blocksAutomaticQuotaReset(auth, time.Now()) {
		return false
	}
	known, safe := quotaRecoveryState(auth.Status, auth.StatusMessage, auth.Unavailable, auth.NextRetryAfter, auth.Quota, auth.LastError)
	if !safe {
		return false
	}
	for _, state := range auth.ModelStates {
		if state == nil {
			continue
		}
		modelKnown, modelSafe := quotaRecoveryState(state.Status, state.StatusMessage, state.Unavailable, state.NextRetryAfter, state.Quota, state.LastError)
		if !modelSafe {
			return false
		}
		known = known || modelKnown
	}
	return known
}

func quotaRecoveryState(status Status, message string, unavailable bool, retry time.Time, quota QuotaState, lastError *Error) (bool, bool) {
	if status != "" && status != StatusActive && status != StatusError {
		return false, false
	}
	reason := strings.TrimSpace(quota.Reason)
	if reason != "" && reason != "quota" && reason != "credential_quota" {
		return false, false
	}
	known := quota.Exceeded && (reason == "quota" || reason == "credential_quota")
	if (quota.Exceeded || reason != "") && !known {
		return false, false
	}
	if lastError != nil && (lastError.Code == ErrorCodeForceCooldown || statusCodeFromResult(lastError) != http.StatusTooManyRequests || isInvalidGrantResultError(lastError) || isCloudflareChallengeResultError(lastError)) {
		return false, false
	}
	if !retry.IsZero() && (!known || quota.NextRecoverAt.IsZero() || retry.After(quota.NextRecoverAt)) {
		return false, false
	}
	if (!quota.NextRecoverAt.IsZero() || quota.BackoffLevel != 0 || unavailable || status == StatusError || lastError != nil) && !known {
		return false, false
	}
	// Nil errors occur on restored or credential-quota propagated states. Do not
	// infer quota ownership from an unrelated diagnostic on those states.
	if lastError == nil {
		switch strings.ToLower(strings.TrimSpace(message)) {
		case "", "quota", "credential_quota", "quota exhausted":
		default:
			return false, false
		}
	}
	return known, true
}

// A public quota diagnostic can outlive or cover independent restrictions.
// Inspect the same retained deadlines the manager uses for routing and restore.
func blocksAutomaticQuotaReset(a *Auth, now time.Time) bool {
	if a.Disabled || a.Status == StatusDisabled || (a.RequiresSignIn && !a.HasValidAccessToken(now)) {
		return true
	}
	if effectiveCredQuotaForceUntil(a).After(now) || effectiveAuthForceUntil(a).After(now) ||
		a.quotaForceCooldownUntil.After(now) || a.nonQuotaCooldownUntil.After(now) ||
		a.credentialRetryAfter.After(now) || a.nonAuthCredentialRetryAfter.After(now) {
		return true
	}
	for _, model := range a.ModelStates {
		if model == nil {
			continue
		}
		if isModelStateActiveCooldown(model.credentialQuotaShadow, now) {
			return true
		}
		for _, layer := range []*ModelState{model, model.credentialQuotaShadow} {
			if layer != nil && (effectiveModelForceUntil(layer).After(now) ||
				layer.nonQuotaCooldownUntil.After(now) || layer.quotaForceCooldownUntil.After(now)) {
				return true
			}
		}
	}
	return false
}

// A store can reconcile credentials during Save. The usage reading authorizes
// recovery only for the credential and account that produced it.
func quotaSaveRetainsAuthority(before, after *Auth) bool {
	if before.Provider != after.Provider || CredentialsChanged(before, after) || !quotaScheduleIdentityMatches(before, after) {
		return false
	}
	for _, key := range []string{"account_uuid", "organization_uuid"} {
		if authMetadataString(before, key) != authMetadataString(after, key) {
			return false
		}
	}
	return before.Disabled == after.Disabled && before.Status == after.Status &&
		before.Unavailable == after.Unavailable && before.NextRetryAfter.Equal(after.NextRetryAfter) &&
		cooldownErrorEqual(before.LastError, after.LastError) &&
		reflect.DeepEqual(before.Quota, after.Quota) &&
		reflect.DeepEqual(before.ModelStates, after.ModelStates) && !blocksAutomaticQuotaReset(after, time.Now())
}

// quotaResetMatchesLocked requires m.mu and the credential mutation gate.
func (m *Manager) quotaResetMatchesLocked(auth, expected *Auth) bool {
	cfg, _ := m.runtimeConfig.Load().(*internalconfig.Config)
	return auth != nil && !(cfg != nil && cfg.Home.Enabled) && auth.ID == expected.ID && auth.Provider == expected.Provider &&
		auth.RegistrationEpoch == expected.RegistrationEpoch && auth.Generation == expected.Generation &&
		QuotaRecoveryCandidate(expected) && QuotaRecoveryCandidate(auth)
}
