// usage_alias_test.go 缓存命中别名归一：真值在 details、兼容别名留 0 的形态。
package upstream

import (
	"strings"
	"testing"
)

// details 里有真命中、三个兼容别名都是 0 → 全部写回真值，且不修改入参 map。
func TestNormalizeUsageCacheAliasesFromDetails(t *testing.T) {
	usage := map[string]any{
		"prompt_tokens":           4000.0,
		"completion_tokens":       100.0,
		"total_tokens":            4100.0,
		"prompt_tokens_details":   map[string]any{"cached_tokens": 3456.0},
		"prompt_cache_hit_tokens": 0.0,
		"cache_read_input_tokens": 0.0,
		"cached_tokens":           0.0,
	}
	got := normalizeUsageCacheAliases(usage)
	for _, key := range []string{"prompt_cache_hit_tokens", "cache_read_input_tokens", "cached_tokens"} {
		if n, _ := num64(got[key]); n != 3456 {
			t.Errorf("%s = %v want 3456", key, got[key])
		}
	}
	details, _ := got["prompt_tokens_details"].(map[string]any)
	if n, _ := num64(details["cached_tokens"]); n != 3456 {
		t.Errorf("details.cached_tokens = %v want 3456", details["cached_tokens"])
	}
	// 入参不能被就地修改（逐帧路径上同一个 map 可能被多处读）。
	if n, _ := num64(usage["cache_read_input_tokens"]); n != 0 {
		t.Errorf("入参被改写：cache_read_input_tokens = %v", usage["cache_read_input_tokens"])
	}
}

// 别名有真值、details 是 0 → 按优先级取别名里的正值回写。
func TestNormalizeUsageCacheAliasesFromFlatAlias(t *testing.T) {
	usage := map[string]any{
		"prompt_tokens_details":   map[string]any{"cached_tokens": 0.0},
		"prompt_cache_hit_tokens": 1200.0,
		"cache_read_input_tokens": 0.0,
	}
	got := normalizeUsageCacheAliases(usage)
	details, _ := got["prompt_tokens_details"].(map[string]any)
	if n, _ := num64(details["cached_tokens"]); n != 1200 {
		t.Errorf("details.cached_tokens = %v want 1200", details["cached_tokens"])
	}
	if n, _ := num64(got["cache_read_input_tokens"]); n != 1200 {
		t.Errorf("cache_read_input_tokens = %v want 1200", got["cache_read_input_tokens"])
	}
}

// 一个正值都没有（真 miss）→ 原样返回，不新增字段、不改类型。
func TestNormalizeUsageCacheAliasesAllZero(t *testing.T) {
	usage := map[string]any{
		"prompt_tokens_details":   map[string]any{"cached_tokens": 0.0},
		"prompt_cache_hit_tokens": 0.0,
		"cache_read_input_tokens": 0.0,
	}
	got := normalizeUsageCacheAliases(usage)
	if _, has := got["input_tokens_details"]; has {
		t.Error("不该凭空造 input_tokens_details")
	}
	if _, has := got["cached_tokens"]; has {
		t.Error("真 miss 时不该新增 cached_tokens")
	}
}

// Responses 形状的嵌套字段原本就在时才回填，且不影响其它键。
func TestNormalizeUsageCacheAliasesKeepsInputDetails(t *testing.T) {
	usage := map[string]any{
		"input_tokens_details":  map[string]any{"cached_tokens": 0.0, "audio_tokens": 7.0},
		"cached_tokens":         900.0,
		"prompt_tokens_details": map[string]any{},
	}
	got := normalizeUsageCacheAliases(usage)
	input, _ := got["input_tokens_details"].(map[string]any)
	if n, _ := num64(input["cached_tokens"]); n != 900 {
		t.Errorf("input_tokens_details.cached_tokens = %v want 900", input["cached_tokens"])
	}
	if n, _ := num64(input["audio_tokens"]); n != 7 {
		t.Errorf("同层其它字段被丢弃：audio_tokens = %v", input["audio_tokens"])
	}
}

// 负数/非数值是垃圾输入，不参与归一。
func TestNormalizeUsageCacheAliasesIgnoresGarbage(t *testing.T) {
	usage := map[string]any{
		"cached_tokens":           -5.0,
		"prompt_cache_hit_tokens": "1200",
	}
	if got := normalizeUsageCacheAliases(usage); got["cached_tokens"] != -5.0 {
		t.Errorf("垃圾值被当成命中：%v", got["cached_tokens"])
	}
}

// 聚合链路：末帧 usage 的别名留 0，出站前应被归一。
func TestAggregateNormalizesUsageCacheAliases(t *testing.T) {
	stream := strings.Join([]string{
		`data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{"content":"hi"}}]}`,
		`data: {"id":"c1","model":"glm-5.2","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4000,"completion_tokens":100,"prompt_tokens_details":{"cached_tokens":3456},"cache_read_input_tokens":0,"cached_tokens":0}}`,
		`data: [DONE]`,
		"",
	}, "\n\n")
	resp, err := Aggregate(strings.NewReader(stream))
	if err != nil {
		t.Fatal(err)
	}
	usage, _ := resp["usage"].(map[string]any)
	if usage == nil {
		t.Fatal("aggregate 丢了 usage")
	}
	if n, _ := num64(usage["cache_read_input_tokens"]); n != 3456 {
		t.Errorf("cache_read_input_tokens = %v want 3456", usage["cache_read_input_tokens"])
	}
	if n, _ := num64(usage["total_tokens"]); n != 4100 {
		t.Errorf("total_tokens = %v want 4100", usage["total_tokens"])
	}
}

// 逐帧链路：末帧 usage 同样归一（下游可能只读流式帧）。
func TestNormalizeFrameNormalizesUsageCacheAliases(t *testing.T) {
	obj := map[string]any{
		"id":      "c1",
		"object":  "chat.completion.chunk",
		"choices": []any{},
		"usage": map[string]any{
			"prompt_tokens":         4000.0,
			"completion_tokens":     100.0,
			"prompt_tokens_details": map[string]any{"cached_tokens": 3456.0},
			"cached_tokens":         0.0,
		},
	}
	out := normalizeFrame(obj)
	usage, _ := out["usage"].(map[string]any)
	if n, _ := num64(usage["cached_tokens"]); n != 3456 {
		t.Errorf("帧内 cached_tokens = %v want 3456", usage["cached_tokens"])
	}
}
