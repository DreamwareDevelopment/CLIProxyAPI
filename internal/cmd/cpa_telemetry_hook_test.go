package cmd

// The telemetry exporter is started per service run, not in main(), so that
// login and other command-line paths never spin one up. That places the hook
// on every function here that actually runs the service, and there is more
// than one of them.
//
// This is the guard against the failure that keeps recurring in this repo:
// two near-identical sites where hooking one looks like hooking both. It is a
// source-level assertion on purpose, mirroring upstream's own
// claude_executor_wire_casing_test.go, because the risk is a NEW or renamed
// entry point silently missing the hook, which no behavioural test on the
// existing two would catch.

import (
	"go/ast"
	"go/parser"
	"go/token"
	"strings"
	"testing"
)

const runSourceFile = "run.go"

// callsIn reports the selector calls (pkg.Fn) made inside a function body.
func callsIn(fn *ast.FuncDecl) map[string]bool {
	found := map[string]bool{}
	ast.Inspect(fn, func(n ast.Node) bool {
		call, ok := n.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		pkg, ok := sel.X.(*ast.Ident)
		if !ok {
			return true
		}
		found[pkg.Name+"."+sel.Sel.Name] = true
		return true
	})
	return found
}

func TestEveryServiceRunStartsTelemetry(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, runSourceFile, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", runSourceFile, err)
	}

	// startsPerRunner is filled during the same walk so the hook count is
	// attributed to the function that owns it.
	startsPerRunner := map[string]int{}
	var runners, hooked []string
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Body == nil {
			continue
		}
		calls := callsIn(fn)
		// Match the Run call by method name on ANY receiver. Keying on the
		// literal identifier "service" would go silently blind the moment an
		// entry point named its variable something else, leaving the count at
		// 2 and passing while the new path had no hook at all.
		//
		// The tradeoff is deliberate. An unrelated future `.Run` call in this
		// file reds this test, which is a prompt to look and adjust the count;
		// the narrow form fails the other way, silently, which is how a
		// missing hook would ship.
		runsService := false
		for c := range calls {
			if strings.HasSuffix(c, ".Run") {
				runsService = true
			}
		}
		if !runsService {
			continue
		}
		runners = append(runners, fn.Name.Name)
		startsPerRunner[fn.Name.Name] = countCalls(fn, "cpatelemetry", "Start")
		if calls["cpatelemetry.Start"] {
			hooked = append(hooked, fn.Name.Name)
		}
	}

	if len(runners) == 0 {
		t.Fatalf("found no function calling service.Run in %s; this test has gone blind", runSourceFile)
	}
	if len(hooked) != len(runners) {
		t.Fatalf("service runners = %v, but only %v start telemetry; an entry point is missing cpatelemetry.Start",
			runners, hooked)
	}
	// Exactly one hook per runner. A whole-file string count was the obvious
	// way to write this and the wrong one: it compares a raw count against an
	// AST-derived list, so the two can disagree for reasons unrelated to a
	// missing hook and produce a red with a misleading message. Attributing
	// the count to the owning function also catches a double Start, which a
	// presence check does not.
	for _, name := range runners {
		if n := startsPerRunner[name]; n != 1 {
			t.Fatalf("%s calls cpatelemetry.Start %d times, want exactly 1", name, n)
		}
	}

	// Both known entry points, so a future third one is a visible diff here.
	if len(runners) != 2 {
		t.Fatalf("expected 2 service entry points, found %d (%v); confirm the new one starts and stops telemetry, then update this count",
			len(runners), runners)
	}
}

// The background entry point returns before the service stops, so its stop
// must be deferred inside the goroutine rather than in the function body, and
// it must be registered after close(doneCh) so it runs before the channel
// closes. Getting that order wrong loses the final flush for anything waiting
// on done.
func TestBackgroundRunStopsTelemetryBeforeDoneCloses(t *testing.T) {
	fset := token.NewFileSet()
	file, err := parser.ParseFile(fset, runSourceFile, nil, 0)
	if err != nil {
		t.Fatalf("parse %s: %v", runSourceFile, err)
	}

	var checked bool
	for _, decl := range file.Decls {
		fn, ok := decl.(*ast.FuncDecl)
		if !ok || fn.Name.Name != "StartServiceBackgroundWithPluginHost" || fn.Body == nil {
			continue
		}
		ast.Inspect(fn, func(n ast.Node) bool {
			lit, ok := n.(*ast.FuncLit)
			if !ok || lit.Body == nil {
				return true
			}
			var order []string
			for _, stmt := range lit.Body.List {
				d, ok := stmt.(*ast.DeferStmt)
				if !ok {
					continue
				}
				switch f := d.Call.Fun.(type) {
				case *ast.Ident:
					order = append(order, f.Name)
				case *ast.SelectorExpr:
					order = append(order, f.Sel.Name)
				}
			}
			ci, si := indexOf(order, "close"), indexOf(order, "stopTelemetry")
			if ci < 0 || si < 0 {
				t.Fatalf("goroutine defers = %v, want both close(doneCh) and stopTelemetry", order)
			}
			if si < ci {
				t.Fatalf("goroutine defers = %v: stopTelemetry is registered before close(doneCh), so it runs after the channel closes and the final flush is lost", order)
			}
			checked = true
			return false
		})
	}
	if !checked {
		t.Fatal("did not find the background goroutine's defers; this test has gone blind")
	}
}

// countCalls counts calls to pkg.fn inside a function body.
func countCalls(fn *ast.FuncDecl, pkg, name string) int {
	n := 0
	ast.Inspect(fn, func(node ast.Node) bool {
		call, ok := node.(*ast.CallExpr)
		if !ok {
			return true
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return true
		}
		id, ok := sel.X.(*ast.Ident)
		if ok && id.Name == pkg && sel.Sel.Name == name {
			n++
		}
		return true
	})
	return n
}

func indexOf(xs []string, want string) int {
	for i, x := range xs {
		if x == want {
			return i
		}
	}
	return -1
}
