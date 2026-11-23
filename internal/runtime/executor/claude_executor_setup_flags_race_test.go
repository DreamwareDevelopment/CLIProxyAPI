package executor

import (
	claudeauth "github.com/router-for-me/CLIProxyAPI/v8/internal/auth/claude"
	"sync"
	"testing"
)

func TestClaudeSetupTokenMetadataFlags(t *testing.T) {
	for _, key := range []string{"skip_account_profile", "is_setup_token", "setup_token"} {
		t.Run(key, func(t *testing.T) {
			auth := newSharedClaudeOAuthAuth("setup-flag")
			for _, tc := range []struct {
				value any
				want  bool
			}{{true, true}, {false, false}, {"true", false}} {
				auth.Metadata[key] = tc.value
				if got := isClaudeSetupToken(auth, "sk-ant-oat-test"); got != tc.want {
					t.Fatalf("setup flag value=%v got=%v want=%v", tc.value, got, tc.want)
				}
			}
		})
	}
}

func TestClaudeSetupTokenMetadataFlagsConcurrent(t *testing.T) {
	for _, key := range []string{"skip_account_profile", "is_setup_token", "setup_token"} {
		t.Run(key, func(t *testing.T) {
			auth := newSharedClaudeOAuthAuth("concurrent-setup-flag")
			auth.Metadata[key] = true
			start := make(chan struct{})
			var wg sync.WaitGroup
			wg.Add(2)
			go func() {
				defer wg.Done()
				<-start
				for i := 0; i < 1000; i++ {
					claudeauth.StoreMetadataString(&auth.Metadata, "account_uuid", "fixture")
				}
			}()
			go func() {
				defer wg.Done()
				<-start
				for i := 0; i < 1000; i++ {
					if !isClaudeSetupToken(auth, "sk-ant-oat-test") {
						t.Error("setup flag disappeared")
						return
					}
				}
			}()
			close(start)
			wg.Wait()
		})
	}
}
