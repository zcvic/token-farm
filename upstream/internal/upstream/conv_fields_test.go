// conv_fields_test.go 会话轮级 body 字段（issue #51 body 侧）。
package upstream

import (
	"encoding/json"
	"testing"
)

// decodeMsgs 解析出站 body 的 messages，失败即 Fatal。
func decodeMsgs(t *testing.T, body []byte) []map[string]any {
	t.Helper()
	var obj struct {
		Messages []map[string]any `json:"messages"`
	}
	if err := json.Unmarshal(body, &obj); err != nil {
		t.Fatalf("unmarshal: %v (body=%s)", err, body)
	}
	return obj.Messages
}

// 只给当前轮最后一条 user 消息补三个字段；历史 user 消息不伪造。
func TestInjectConversationFieldsCurrentTurnOnly(t *testing.T) {
	body := []byte(`{"model":"m","messages":[
		{"role":"system","content":"s"},
		{"role":"user","content":"历史问题"},
		{"role":"assistant","content":"历史回答"},
		{"role":"user","content":"这轮的问题"}
	]}`)
	out := injectConversationFields(body, "req-1")
	msgs := decodeMsgs(t, out)
	if len(msgs) != 4 {
		t.Fatalf("messages=%d want 4", len(msgs))
	}
	if v, _ := msgs[1]["conversationRequestId"].(string); v != "" {
		t.Errorf("历史 user 消息被补了轮级 ID %q（网关无从得知，不能编造）", v)
	}
	cur := msgs[3]
	if got, _ := cur["conversationRequestId"].(string); got != "req-1" {
		t.Errorf("conversationRequestId=%q want req-1", got)
	}
	if got, _ := cur["startsNewUserRequest"].(bool); !got {
		t.Errorf("startsNewUserRequest=%v want true", cur["startsNewUserRequest"])
	}
	if got, _ := cur["agent"].(string); got != "cli" {
		t.Errorf("agent=%q want cli", got)
	}
	if got, _ := cur["content"].(string); got != "这轮的问题" {
		t.Errorf("content=%q 被改写", got)
	}
}

// 客户端自带 conversationRequestId 时原值优先，不覆盖（也不重复写其他字段）。
func TestInjectConversationFieldsClientValueWins(t *testing.T) {
	body := []byte(`{"messages":[{"role":"user","content":"x",
		"conversationRequestId":"client-req","agent":"desktop"}]}`)
	out := injectConversationFields(body, "gateway-req")
	msgs := decodeMsgs(t, out)
	if got, _ := msgs[0]["conversationRequestId"].(string); got != "client-req" {
		t.Errorf("conversationRequestId=%q want client-req（原值优先）", got)
	}
	if got, _ := msgs[0]["agent"].(string); got != "desktop" {
		t.Errorf("agent=%q want desktop（显式值不覆盖）", got)
	}
}

// 空 ID / 坏 body / 没有 messages / 没有 user 消息：原样返回，不二次错误化。
func TestInjectConversationFieldsNoop(t *testing.T) {
	cases := map[string][]byte{
		"empty id":      []byte(`{"messages":[{"role":"user","content":"x"}]}`),
		"bad json":      []byte(`{"messages":`),
		"no messages":   []byte(`{"model":"m"}`),
		"no user":       []byte(`{"messages":[{"role":"assistant","content":"x"}]}`),
		"messages null": []byte(`{"messages":null}`),
	}
	for name, in := range cases {
		id := "req-1"
		if name == "empty id" {
			id = ""
		}
		out := injectConversationFields(in, id)
		if string(out) != string(in) {
			t.Errorf("%s: body 被改动\nin =%s\nout=%s", name, in, out)
		}
	}
}

// 走完整 prepareBody 后字段仍在：前面的 body 改写（脱敏/额度/缓存键）不保证保留
// 未知字段，注入必须在它们之后。
func TestPrepareBodyKeepsConversationFields(t *testing.T) {
	c := New()
	body := []byte(`{"model":"deepseek-v4.1-flash","max_tokens":32,"messages":[
		{"role":"user","content":"你好"}
	]}`)
	out := c.prepareBody(body, "cn", "uid-1", "conv-1", "req-9")
	msgs := decodeMsgs(t, out)
	if len(msgs) != 1 {
		t.Fatalf("messages=%d want 1", len(msgs))
	}
	if got, _ := msgs[0]["conversationRequestId"].(string); got != "req-9" {
		t.Errorf("conversationRequestId=%q want req-9（prepareBody 把它弄丢了）", got)
	}
}
