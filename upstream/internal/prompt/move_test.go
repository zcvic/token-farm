package prompt

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestMoveSystemToUserMovesTextAndKeepsUserContent(t *testing.T) {
	body := []byte(`{"model":"m","messages":[` +
		`{"role":"system","content":"PERSONA-A"},` +
		`{"role":"developer","content":"PERSONA-B"},` +
		`{"role":"user","content":"hello"},` +
		`{"role":"assistant","content":"hi"}]}`)
	out, ok := MoveSystemToUser(body)
	if !ok {
		t.Fatal("有 system 文本与 user 消息时应成功")
	}
	var req struct {
		Messages []struct {
			Role    string `json:"role"`
			Content string `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(out, &req); err != nil {
		t.Fatalf("输出不是合法 JSON: %v", err)
	}
	if len(req.Messages) != 3 {
		t.Fatalf("两条 system 应合并为一条中性 system，got %d 条", len(req.Messages))
	}
	if req.Messages[0].Role != "system" || req.Messages[0].Content != Degraded {
		t.Fatalf("首条应换成中性提示词，got %+v", req.Messages[0])
	}
	if req.Messages[1].Role != "user" {
		t.Fatalf("第二条应仍是 user，got %+v", req.Messages[1])
	}
	if !strings.Contains(req.Messages[1].Content, "PERSONA-A") ||
		!strings.Contains(req.Messages[1].Content, "PERSONA-B") ||
		!strings.HasSuffix(req.Messages[1].Content, "hello") {
		t.Fatalf("system 文本应原样搬到 user 前部、原 user 内容在尾部，got %q", req.Messages[1].Content)
	}
	if req.Messages[2].Content != "hi" {
		t.Fatalf("后续消息应逐字不动，got %+v", req.Messages[2])
	}
}

func TestMoveSystemToUserBails(t *testing.T) {
	cases := map[string]string{
		"非 JSON":     `not json`,
		"无 messages": `{}`,
		"没有 system":  `{"messages":[{"role":"user","content":"hi"}]}`,
		"没有 user":    `{"messages":[{"role":"system","content":"s"}]}`,
		// 数组 content 从 0065 起**不再**算放弃条件（issue #479），改由下面的
		// TestMoveSystemToUserSupportsContentParts 覆盖。
		"user content 为 null": `{"messages":[{"role":"system","content":"s"},` +
			`{"role":"user","content":null}]}`,
	}
	for name, body := range cases {
		if _, ok := MoveSystemToUser([]byte(body)); ok {
			t.Errorf("%s 应保守返回 false", name)
		}
	}
}

// TestMoveSystemToUserSupportsContentParts issue #479：Codex / 多模态客户端的
// user content 是 content parts 数组，旧的字符串断言会让 11-128 抢救整条失效。
func TestMoveSystemToUserSupportsContentParts(t *testing.T) {
	body := []byte(`{"model":"m","messages":[` +
		`{"role":"system","content":"PERSONA-A"},` +
		`{"role":"user","content":[` +
		`{"type":"text","text":"hello"},` +
		`{"type":"image_url","image_url":{"url":"data:image/png;base64,AAAA"}}]},` +
		`{"role":"assistant","content":"hi"}]}`)
	out, ok := MoveSystemToUser(body)
	if !ok {
		t.Fatal("数组 content 也应能完成 system→user 搬运")
	}
	var req struct {
		Messages []struct {
			Role    string `json:"role"`
			Content any    `json:"content"`
		} `json:"messages"`
	}
	if err := json.Unmarshal(out, &req); err != nil {
		t.Fatalf("输出不是合法 JSON: %v", err)
	}
	if len(req.Messages) != 3 {
		t.Fatalf("system 应合并为一条中性 system，got %d 条", len(req.Messages))
	}
	if req.Messages[0].Role != "system" || req.Messages[0].Content != Degraded {
		t.Fatalf("首条应换成中性提示词，got %+v", req.Messages[0])
	}
	parts, ok := req.Messages[1].Content.([]any)
	if !ok {
		t.Fatalf("user content 应保持数组形态，got %T", req.Messages[1].Content)
	}
	if len(parts) != 3 {
		t.Fatalf("应插入 1 个 text part（原 2 个 part 保留），got %d", len(parts))
	}
	first, _ := parts[0].(map[string]any)
	if first["type"] != "text" || !strings.Contains(first["text"].(string), "PERSONA-A") {
		t.Fatalf("第一个 part 应是携带 system 文本的 text part，got %+v", parts[0])
	}
	second, _ := parts[1].(map[string]any)
	if second["type"] != "text" || second["text"] != "hello" {
		t.Fatalf("原有 text part 应逐字保留，got %+v", parts[1])
	}
	third, _ := parts[2].(map[string]any)
	img, _ := third["image_url"].(map[string]any)
	if third["type"] != "image_url" || img["url"] != "data:image/png;base64,AAAA" {
		t.Fatalf("图片 part 应逐字保留，got %+v", parts[2])
	}
}
