package upstream

import (
	"encoding/json"
	"testing"
)

func rolesOf(msgs []any) []string {
	out := make([]string, 0, len(msgs))
	for _, m := range msgs {
		role := ""
		if mm, ok := m.(map[string]any); ok {
			role, _ = mm["role"].(string)
		}
		out = append(out, role)
	}
	return out
}

func toolCallMsg(id string) map[string]any {
	return map[string]any{"role": "assistant", "tool_calls": []any{
		map[string]any{"id": id, "type": "function", "function": map[string]any{"name": "f", "arguments": "{}"}},
	}}
}

func toolResultMsg(id, content string) map[string]any {
	return map[string]any{"role": "tool", "tool_call_id": id, "content": content}
}

// TestReorderEarlyToolResultsSingle 结果排在声明它的 assistant 之前 → 挪到声明之后，
// 内容与顺序（同组相对序）都不变。
func TestReorderEarlyToolResultsSingle(t *testing.T) {
	messages := []any{toolResultMsg("c1", "r1"), toolCallMsg("c1")}
	out, changed := reorderEarlyToolResults(messages)
	if !changed {
		t.Fatal("early tool result should be reordered")
	}
	if got, want := rolesOf(out), []string{"assistant", "tool"}; len(got) != len(want) ||
		got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("roles = %v, want %v", got, want)
	}
	if out[1].(map[string]any)["content"] != "r1" {
		t.Fatalf("content changed: %#v", out[1])
	}
}

// TestReorderEarlyToolResultsNormalOrderNoop 本来顺序就对的请求零改动零分配。
func TestReorderEarlyToolResultsNormalOrderNoop(t *testing.T) {
	messages := []any{toolCallMsg("c1"), toolResultMsg("c1", "r1")}
	out, changed := reorderEarlyToolResults(messages)
	if changed {
		t.Fatal("normal order must not be reordered")
	}
	if &out[0] != &messages[0] {
		t.Fatal("unchanged request should return the same slice")
	}
}

// TestReorderEarlyToolResultsMultipleKeepsRelativeOrder 同一声明下的多条早到结果按
// 原相对顺序补在 assistant（及其后连续 tool 结果）之后。
func TestReorderEarlyToolResultsMultipleKeepsRelativeOrder(t *testing.T) {
	messages := []any{
		toolResultMsg("c1", "early-1"),
		toolResultMsg("c2", "early-2"),
		toolCallMsg("c1"),
		toolCallMsg("c2"),
	}
	out, changed := reorderEarlyToolResults(messages)
	if !changed {
		t.Fatal("expected reorder")
	}
	got := rolesOf(out)
	want := []string{"assistant", "tool", "assistant", "tool"}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("roles = %v, want %v", got, want)
		}
	}
	// c1 的声明在 c2 之前：c1 的早到结果先落地，c2 的跟在 c2 声明之后。
	if out[1].(map[string]any)["content"] != "early-1" {
		t.Fatalf("c1 result misplaced: %#v", out[1])
	}
	if out[3].(map[string]any)["content"] != "early-2" {
		t.Fatalf("c2 result misplaced: %#v", out[3])
	}
}

// TestReorderEarlyToolResultsIDReuseAcrossTurns 跨轮复用 call_0：第一轮顺序正确，
// 第二轮结果早到。按「已声明未应答」计数，第一轮的结果不能被搬到最后。
func TestReorderEarlyToolResultsIDReuseAcrossTurns(t *testing.T) {
	messages := []any{
		toolCallMsg("call_0"),          // 第一轮声明
		toolResultMsg("call_0", "one"), // 第一轮结果，顺序正确
		map[string]any{"role": "user", "content": "next"},
		toolResultMsg("call_0", "two"), // 第二轮结果，早到
		toolCallMsg("call_0"),          // 第二轮声明
	}
	out, changed := reorderEarlyToolResults(messages)
	if !changed {
		t.Fatal("expected reorder for the second round")
	}
	if got, want := rolesOf(out), []string{"assistant", "tool", "user", "assistant", "tool"}; len(got) != len(want) {
		t.Fatalf("roles = %v, want %v", got, want)
	} else {
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("roles = %v, want %v", got, want)
			}
		}
	}
	if out[1].(map[string]any)["content"] != "one" {
		t.Fatalf("first-round result was moved: %#v", out[1])
	}
	if out[4].(map[string]any)["content"] != "two" {
		t.Fatalf("second-round result misplaced: %#v", out[4])
	}
}

// TestReorderEarlyToolResultsAppendsAfterWholeGroup 早到结果补在声明之后**连续 tool
// 结果之后**，不能插进正常结果中间（同批内原相对顺序保持）。
func TestReorderEarlyToolResultsAppendsAfterWholeGroup(t *testing.T) {
	messages := []any{
		toolResultMsg("c2", "early"),
		toolCallMsg("c1"),
		toolResultMsg("c1", "normal"),
		toolCallMsg("c2"),
	}
	out, changed := reorderEarlyToolResults(messages)
	if !changed {
		t.Fatal("expected reorder")
	}
	if got, want := rolesOf(out), []string{"assistant", "tool", "assistant", "tool"}; len(got) != len(want) {
		t.Fatalf("roles = %v, want %v", got, want)
	} else {
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("roles = %v, want %v", got, want)
			}
		}
	}
	if out[1].(map[string]any)["content"] != "normal" {
		t.Fatalf("normal result should stay directly after its call: %#v", out[1])
	}
	if out[3].(map[string]any)["content"] != "early" {
		t.Fatalf("early result should follow the whole group: %#v", out[3])
	}
}

// TestReorderEarlyToolResultsOrphanUntouched 前后都找不到声明者的孤儿结果不动，
// 交给 cleanupOrphanToolCalls 处理。
func TestReorderEarlyToolResultsOrphanUntouched(t *testing.T) {
	messages := []any{toolResultMsg("ghost", "r"), toolCallMsg("c1"), toolResultMsg("c1", "ok")}
	out, changed := reorderEarlyToolResults(messages)
	if changed {
		t.Fatal("orphan-only request should not be reordered")
	}
	if &out[0] != &messages[0] {
		t.Fatal("expected original slice")
	}
}

// TestReorderEarlyToolResultsNoDeclarations 没有 assistant.tool_calls 时零改动。
func TestReorderEarlyToolResultsNoDeclarations(t *testing.T) {
	messages := []any{toolResultMsg("c1", "r"), map[string]any{"role": "user", "content": "hi"}}
	_, changed := reorderEarlyToolResults(messages)
	if changed {
		t.Fatal("no declarations means nothing to reorder")
	}
}

// TestPrepareBodyReordersEarlyToolResults 全链路：早到的 tool 结果经 PrepareBodyOpt
// 后必须排在声明它的 assistant 之后（此前 repack 当普通消息直通、cleanup 又判
// 「配对完整」，倒置一路带到上游撞 11148）。
func TestPrepareBodyReordersEarlyToolResults(t *testing.T) {
	body := `{"model":"glm-5.2","messages":[
		{"role":"tool","tool_call_id":"c1","content":"early"},
		{"role":"assistant","tool_calls":[{"id":"c1","type":"function","function":{"name":"f","arguments":"{}"}}]}
	]}`
	out := PrepareBodyOpt([]byte(body), false)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	msgs := obj["messages"].([]any)
	if got, want := rolesOf(msgs), []string{"assistant", "tool"}; len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("roles = %v, want %v", got, want)
	}
	if msgs[1].(map[string]any)["content"] != "early" {
		t.Fatalf("content lost: %#v", msgs[1])
	}
}
