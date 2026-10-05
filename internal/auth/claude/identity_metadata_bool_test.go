package claude

import "testing"

func TestReadMetadataBool(t *testing.T) {
	if ReadMetadataBool(nil, "flag") {
		t.Fatal("nil metadata was true")
	}
	var absent map[string]any
	if ReadMetadataBool(&absent, "flag") {
		t.Fatal("nil map was true")
	}
	for _, tc := range []struct {
		value any
		want  bool
	}{{true, true}, {false, false}, {"true", false}, {1, false}, {nil, false}} {
		metadata := map[string]any{"flag": tc.value}
		if got := ReadMetadataBool(&metadata, "flag"); got != tc.want {
			t.Fatalf("value=%v got=%v want=%v", tc.value, got, tc.want)
		}
		if ReadMetadataBool(&metadata, "missing") {
			t.Fatal("missing flag was true")
		}
	}
}
