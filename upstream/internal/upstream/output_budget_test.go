// output_budget_test.go 输出预算下限的四类不猜情形与目录封顶。
package upstream

import (
	"encoding/json"
	"testing"
)

// outBudget 跑完整出站管线后读回 max_tokens（存在性与取值）。
func outBudget(t *testing.T, body string) (float64, bool) {
	t.Helper()
	out := PrepareBodyOptWithEffortsAndDefault([]byte(body), false, nil, nil)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatalf("unmarshal prepared body: %v", err)
	}
	v, ok := obj["max_tokens"].(float64)
	return v, ok
}

// 各档位按表抬到下限；已经是整数形态写回（不会被写成 8192.0）。
func TestOutputBudgetRaisedByEffort(t *testing.T) {
	cases := []struct {
		effort string
		want   float64
	}{
		{"low", 4096},
		{"medium", 6144},
		{"high", 8192},
		{"xhigh", 12288},
		{"max", 16384},
	}
	for _, tc := range cases {
		body := `{"model":"glm-5.2","messages":[],"reasoning_effort":"` + tc.effort + `","max_tokens":1024}`
		got, ok := outBudget(t, body)
		if !ok || got != tc.want {
			t.Errorf("effort=%s max_tokens=%v want %v", tc.effort, got, tc.want)
		}
	}
}

// 客户端 camelCase 档位同样识别。
func TestOutputBudgetCamelEffort(t *testing.T) {
	got, _ := outBudget(t, `{"model":"glm-5.2","messages":[],"reasoningEffort":"high","max_tokens":2048}`)
	if got != 8192 {
		t.Errorf("max_tokens=%v want 8192", got)
	}
}

// 额度已经够大时不改（只抬不压）。
func TestOutputBudgetKeepsLargerLimit(t *testing.T) {
	got, _ := outBudget(t, `{"model":"glm-5.2","messages":[],"reasoning_effort":"high","max_tokens":32000}`)
	if got != 32000 {
		t.Errorf("max_tokens=%v want 32000", got)
	}
}

// 没给显式额度 → 不新增字段（走上游/目录默认）。
func TestOutputBudgetNoExplicitLimit(t *testing.T) {
	out := PrepareBodyOptWithEffortsAndDefault(
		[]byte(`{"model":"glm-5.2","messages":[],"reasoning_effort":"max"}`), false, nil, nil)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatal(err)
	}
	if _, has := obj["max_tokens"]; has {
		t.Errorf("未给额度却写入了 max_tokens=%v", obj["max_tokens"])
	}
}

// 档位未知 → 不猜。
func TestOutputBudgetUnknownEffort(t *testing.T) {
	got, _ := outBudget(t, `{"model":"glm-5.2","messages":[],"reasoning_effort":"turbo","max_tokens":1024}`)
	if got != 1024 {
		t.Errorf("未知档位被改写为 %v", got)
	}
}

// 探活级额度（<16）不动。
func TestOutputBudgetProbeLevel(t *testing.T) {
	got, _ := outBudget(t, `{"model":"glm-5.2","messages":[],"reasoning_effort":"max","max_tokens":8}`)
	if got != 8 {
		t.Errorf("探活额度被改写为 %v", got)
	}
}

// 0 的语义是「未设置」，不是「额度为零」，不翻成下限。
func TestOutputBudgetZeroLimit(t *testing.T) {
	got, _ := outBudget(t, `{"model":"glm-5.2","messages":[],"reasoning_effort":"max","max_tokens":0}`)
	if got != 0 {
		t.Errorf("max_tokens=0 被改写为 %v", got)
	}
}

// 显式关思考 → 额度与思考无关，不动（非 deepseek 模型不会被 injectThinking 删档位，
// 正好覆盖「disabled + 仍带 effort」的组合）。
func TestOutputBudgetThinkingDisabled(t *testing.T) {
	got, _ := outBudget(t,
		`{"model":"glm-5.2","messages":[],"thinking":{"type":"disabled"},"reasoning_effort":"max","max_tokens":1024}`)
	if got != 1024 {
		t.Errorf("关思考后额度被改写为 %v", got)
	}
}

// 目录 maxOutputTokens 更小时以目录封顶，不把上限抬过模型能力。
func TestOutputBudgetCatalogCap(t *testing.T) {
	obj := map[string]any{"max_tokens": int64(1024)}
	ensureMinOutputBudget(obj, effortOutputFloor["max"], 6000)
	if got := obj["max_tokens"].(int64); got != 6000 {
		t.Errorf("目录封顶 = %v want 6000", got)
	}
	// 目录值比当前额度还小 → 不动（只抬不压）。
	obj = map[string]any{"max_tokens": int64(2048)}
	ensureMinOutputBudget(obj, effortOutputFloor["max"], 2048)
	if got := obj["max_tokens"].(int64); got != 2048 {
		t.Errorf("目录封顶反向压低了额度: %v", got)
	}
}

// 小数 / 字符串形态的 max_tokens 属垃圾输入，原样留给上游报错。
func TestOutputBudgetNonIntegerLimit(t *testing.T) {
	if got, ok := outputBudgetLimit(map[string]any{"max_tokens": 1024.5}); ok {
		t.Errorf("小数被当成额度: %v", got)
	}
	if got, ok := outputBudgetLimit(map[string]any{"max_tokens": "1024"}); ok {
		t.Errorf("字符串被当成额度: %v", got)
	}
}
