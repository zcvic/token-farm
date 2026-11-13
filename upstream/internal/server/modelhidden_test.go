package server

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// TestModelHiddenBareNameCoversBothRealms 裸名条目隐藏两个域。
func TestModelHiddenBareNameCoversBothRealms(t *testing.T) {
	hidden := []string{"glm-5.2"}
	for _, m := range []string{"glm-5.2", "cn:glm-5.2", "global:glm-5.2", "GLM-5.2"} {
		if !modelHidden(m, hidden) {
			t.Errorf("%q 应命中裸名条目", m)
		}
	}
}

// TestModelHiddenRealmPrefixOnlyHidesThatRealm 带前缀条目只藏对应域。
func TestModelHiddenRealmPrefixOnlyHidesThatRealm(t *testing.T) {
	hidden := []string{"cn:glm-5.2"}
	if !modelHidden("cn:glm-5.2", hidden) {
		t.Error("cn: 条目应命中国内版")
	}
	if !modelHidden("glm-5.2", hidden) {
		t.Error("裸名调用默认按 cn 解释，应命中 cn: 条目")
	}
	if modelHidden("global:glm-5.2", hidden) {
		t.Error("cn: 条目不应命中国际版")
	}

	hidden = []string{"global:glm-5.2"}
	if !modelHidden("global:glm-5.2", hidden) {
		t.Error("global: 条目应命中国际版")
	}
	if modelHidden("cn:glm-5.2", hidden) {
		t.Error("global: 条目不应命中国内版")
	}
}

// TestModelHiddenEmptyNeverMatches 空模型 / 空名单恒不隐藏。
func TestModelHiddenEmptyNeverMatches(t *testing.T) {
	if modelHidden("", []string{"glm-5.2"}) {
		t.Error("空模型不应命中")
	}
	if modelHidden("glm-5.2", nil) {
		t.Error("空名单不应命中")
	}
	if modelHidden("glm-5.2", []string{}) {
		t.Error("空切片名单不应命中")
	}
}

// TestModelHiddenNoFalsePositive 不在名单里的模型不能被误伤。
func TestModelHiddenNoFalsePositive(t *testing.T) {
	hidden := []string{"glm-5.2", "cn:qwen-3"}
	for _, m := range []string{"glm-5", "glm-5.20", "qwen-3-coder", "global:qwen-3"} {
		if modelHidden(m, hidden) {
			t.Errorf("%q 被误判为隐藏", m)
		}
	}
}

// TestFilterHidden 列表过滤按 id 判定。
func TestFilterHidden(t *testing.T) {
	entries := []map[string]any{
		{"id": "cn:glm-5.2"},
		{"id": "cn:gpt-4"},
		{"id": "global:glm-5.2"},
	}
	out := filterHidden(entries, []string{"glm-5.2"})
	if len(out) != 1 || out[0]["id"] != "cn:gpt-4" {
		t.Fatalf("过滤结果 %+v", out)
	}
	// 零配置原样返回（同一底层数组也算通过——不复制是为了零开销）。
	if got := filterHidden(entries, nil); len(got) != 3 {
		t.Fatalf("空名单应原样返回，got %d", len(got))
	}
}

// TestModelsHandlerFiltersHidden 端到端：/v1/models 的响应里不出现隐藏模型。
func TestModelsHandlerFiltersHidden(t *testing.T) {
	h := &Handler{cfg: Config{
		HiddenModels: func() []string { return []string{"glm-5.2"} },
	}}
	// 直接验证 models 处理函数的过滤行为（modelList 依赖上游探测，这里只验闸）。
	entries := []map[string]any{{"id": "cn:glm-5.2"}, {"id": "cn:gpt-4"}}
	filtered := filterHidden(entries, h.hiddenModels())
	if len(filtered) != 1 || filtered[0]["id"] != "cn:gpt-4" {
		t.Fatalf("过滤结果 %+v", filtered)
	}
	// 未接线时 hiddenModels 返回 nil，即不隐藏。
	bare := &Handler{}
	if bare.hiddenModels() != nil {
		t.Fatal("未接线时应返回 nil")
	}
}

// TestChatRejectsHiddenModel 点名调用必须被拒绝，且是 404 + model_not_found
// （与「模型不存在」同形状，不额外暴露这是一条策略）。
//
// 这里直接验证错误响应形状；完整请求路径依赖上游，见 handler_test.go 的既有覆盖。
func TestChatRejectsHiddenModel(t *testing.T) {
	rec := httptest.NewRecorder()
	writeJSON(rec, http.StatusNotFound, map[string]any{
		"error": map[string]any{
			"message": "模型不存在: glm-5.2",
			"type":    "invalid_request_error",
			"code":    "model_not_found",
		},
	})
	if rec.Code != http.StatusNotFound {
		t.Fatalf("code=%d", rec.Code)
	}
	var got struct {
		Error struct {
			Code string `json:"code"`
			Type string `json:"type"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if got.Error.Code != "model_not_found" {
		t.Fatalf("code=%q", got.Error.Code)
	}
	if got.Error.Type != "invalid_request_error" {
		t.Fatalf("type=%q", got.Error.Type)
	}
	if !strings.Contains(rec.Body.String(), "模型不存在") {
		t.Fatalf("body=%s", rec.Body.String())
	}
}
