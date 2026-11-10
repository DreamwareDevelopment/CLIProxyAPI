package config

import (
	"gopkg.in/yaml.v3"
	"reflect"
	"testing"
)

// Synthetic deployment-shaped configuration. No production credentials or file
// reads are involved; this rehearses the parser and explicit v8 migration only.
func TestCPALegacyConfigMigration(t *testing.T) {
	raw := []byte(`host: 127.0.0.1
port: 8317
auth-dir: /data/auths
api-keys: [synthetic-client]
request-retry: 2
passthrough-headers: true
routing:
  strategy: round-robin
  session-affinity: true
claude-header-defaults:
  user-agent: "claude-cli/2.1.280 (external, cli)"
  package-version: "0.112.1"
  runtime-version: "v26.3.0"
  os: MacOS
  arch: arm64
`)
	before, err := ParseConfigBytes(raw)
	if err != nil {
		t.Fatal(err)
	}
	migrated, changed, err := NormalizeConfigLayout(raw, true)
	if err != nil {
		t.Fatal(err)
	}
	if !changed {
		t.Fatal("legacy configuration was not migrated")
	}
	if err := ValidateV8Config(migrated); err != nil {
		t.Fatal(err)
	}
	var doc yaml.Node
	if err := yaml.Unmarshal(migrated, &doc); err != nil {
		t.Fatal(err)
	}
	authDir := yamlPath(doc.Content[0], "oauth.auth-dir")
	if authDir == nil || authDir.Value != "/data/auths" {
		t.Fatal("migration lost canonical auth directory")
	}
	after, err := ParseConfigBytes(migrated)
	if err != nil {
		t.Fatal(err)
	}
	after.OAuthOnlyFields = nil
	if !reflect.DeepEqual(before, after) {
		t.Fatal("migration changed effective CPA configuration")
	}
	if after.Host != "127.0.0.1" || after.Port != 8317 || after.AuthDir != "/data/auths" || after.RequestRetry != 2 || !after.PassthroughHeaders || !reflect.DeepEqual(after.APIKeys, []string{"synthetic-client"}) {
		t.Fatal("deployment-critical configuration did not survive")
	}
	if after.ClaudeHeaderDefaults.UserAgent != "claude-cli/2.1.280 (external, cli)" {
		t.Fatal("Claude baseline lost")
	}
}
