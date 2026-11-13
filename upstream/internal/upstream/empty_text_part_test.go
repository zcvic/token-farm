package upstream

import (
	"encoding/json"
	"testing"
)

// TestGuardEmptyTextParts 上游对每个 text 段判零长度：空串 / null / 缺失三种直接被
// 拒（400 + code 11151），"   " 放行、未识别 type 被忽略、非数组对象不嗅探。
// 修法是只补值不删段（填一个空格），因为删段会改段落数量与下标。
func TestGuardEmptyTextParts(t *testing.T) {
	cases := []struct {
		name string
		in   string
		// wantText 断言 content 数组第 0 段的 text 值；wantAbsent 表示该段不应有 text 键
		wantText   any
		wantAbsent bool
	}{
		{
			name:     "空串补空格",
			in:       `[{"type":"text","text":""}]`,
			wantText: " ",
		},
		{
			name:     "null 补空格",
			in:       `[{"type":"text","text":null}]`,
			wantText: " ",
		},
		{
			name:     "缺失 text 键补空格",
			in:       `[{"type":"text"}]`,
			wantText: " ",
		},
		{
			name:     "纯空白放行（上游只判零长度）",
			in:       `[{"type":"text","text":"   "}]`,
			wantText: "   ",
		},
		{
			name:     "未识别 type 不动",
			in:       `[{"type":"textarea","text":""}]`,
			wantText: "",
		},
		{
			name:       "非 text 段原样保留（图片）",
			in:         `[{"type":"image_url","image_url":{"url":"http://x"}}]`,
			wantAbsent: true,
		},
		{
			name:     "非字符串 text 不动（无实测依据不猜）",
			in:       `[{"type":"text","text":123}]`,
			wantText: float64(123),
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			obj := decodeMessages(t, `{"messages":[{"role":"user","content":`+c.in+`}]}`)
			msgs := obj["messages"].([]any)
			guardEmptyTextParts(msgs)
			parts, ok := msgs[0].(map[string]any)["content"].([]any)
			if !ok {
				t.Fatalf("content 不是数组: %T", msgs[0].(map[string]any)["content"])
			}
			if len(parts) != 1 {
				t.Fatalf("段数变了：%d want 1（补位不得删段）", len(parts))
			}
			part, ok := parts[0].(map[string]any)
			if !ok {
				t.Fatalf("第 0 段不是对象: %v", parts[0])
			}
			got, has := part["text"]
			if c.wantAbsent {
				if has {
					t.Errorf("该段不该有 text 键: %v", got)
				}
				return
			}
			if !has || got != c.wantText {
				t.Errorf("text=%v (has=%v) want %v", got, has, c.wantText)
			}
		})
	}
}

// TestGuardEmptyTextPartsStringSniff 字符串 content 去前导空白后以 `[` 开头时，
// 上游会把它当 parts 数组解析——工具结果里抓到的表单/按钮数组正是这个形状，于是
// 一段「数据」被嗅探成空文本段。这里按同口径嗅探并补位后重新序列化。
func TestGuardEmptyTextPartsStringSniff(t *testing.T) {
	raw := `  [{"type":"text","text":""},{"type":"text","text":"ok"}]`
	obj := decodeMessages(t, `{"messages":[{"role":"tool","content":`+jsonString(raw)+`}]}`)
	msgs := obj["messages"].([]any)
	guardEmptyTextParts(msgs)

	content, ok := msgs[0].(map[string]any)["content"].(string)
	if !ok {
		t.Fatalf("嗅探命中后应回写成字符串，got %T", msgs[0].(map[string]any)["content"])
	}
	var parts []any
	if err := json.Unmarshal([]byte(content), &parts); err != nil {
		t.Fatalf("回写内容不是合法 JSON: %v (%q)", err, content)
	}
	if got := parts[0].(map[string]any)["text"]; got != " " {
		t.Errorf("嗅探出的空段应补空格，got %v", got)
	}
	if got := parts[1].(map[string]any)["text"]; got != "ok" {
		t.Errorf("非空段不该动，got %v", got)
	}
}

// TestGuardEmptyTextPartsStringNotSniffed 这些字符串上游不会当 parts：普通文本、
// 以 [ 开头但解析失败的串、空数组、元素不是对象的数组。一律原样保留。
func TestGuardEmptyTextPartsStringNotSniffed(t *testing.T) {
	for _, raw := range []string{
		`hello world`,
		`[not json`,
		`[]`,
		`["a","b"]`,
		`  {"type":"text","text":""}`, // 对象不嗅探
	} {
		t.Run(raw, func(t *testing.T) {
			obj := decodeMessages(t, `{"messages":[{"role":"user","content":`+jsonString(raw)+`}]}`)
			msgs := obj["messages"].([]any)
			guardEmptyTextParts(msgs)
			if got := msgs[0].(map[string]any)["content"]; got != raw {
				t.Errorf("不该改动，got %q want %q", got, raw)
			}
		})
	}
}

// TestGuardEmptyTextPartsNoChangeKeepsBody 无空段时整条消息一字不动——这是「普通
// 请求零影响」的判据（含 content 为 null / 字符串 / 缺失三种形态）。
func TestGuardEmptyTextPartsNoChangeKeepsBody(t *testing.T) {
	body := `{"messages":[{"role":"user","content":"hi"},{"role":"assistant","content":null},{"role":"tool"}]}`
	obj := decodeMessages(t, body)
	msgs := obj["messages"].([]any)
	guardEmptyTextParts(msgs)
	if got, _ := msgs[0].(map[string]any)["content"].(string); got != "hi" {
		t.Errorf("普通字符串被改动: %v", got)
	}
	if v, has := msgs[1].(map[string]any)["content"]; !has || v != nil {
		t.Errorf("null content 被改动: %v has=%v", v, has)
	}
	if _, has := msgs[2].(map[string]any)["content"]; has {
		t.Error("缺失 content 不该被新建")
	}
}

// TestMirrorUpstreamReasoning 六家族（kimi/glm/minimax/mimo/step/space-bunny）
// 按 `reasoning` 读历史：客户端只发 reasoning_content 时镜像过去，否则上游认为
// assistant 没有思考、多轮一致性校验会挂。
func TestMirrorUpstreamReasoning(t *testing.T) {
	families := []string{"kimi-k3.1-pro", "glm-5.2", "MiniMax-M2", "mimo-vl", "step-3", "space-bunny-x"}
	for _, model := range families {
		t.Run(model, func(t *testing.T) {
			obj := decodeMessages(t, `{"model":`+jsonString(model)+`,"messages":[`+
				`{"role":"user","content":"q"},`+
				`{"role":"assistant","content":"a","reasoning_content":"thought"}]}`)
			mirrorUpstreamReasoning(obj)
			msg := obj["messages"].([]any)[1].(map[string]any)
			if got, _ := msg["reasoning"].(string); got != "thought" {
				t.Errorf("reasoning=%v want thought（镜像自 reasoning_content）", msg["reasoning"])
			}
			if got, _ := msg["reasoning_content"].(string); got != "thought" {
				t.Errorf("原字段不该被删: %v", msg["reasoning_content"])
			}
		})
	}
}

// TestMirrorUpstreamReasoningRealmPrefix 入参带 realm 前缀时同样命中（防御：出站
// 前 handler 已剥前缀，这里只是不因前缀漏判）。
func TestMirrorUpstreamReasoningRealmPrefix(t *testing.T) {
	obj := decodeMessages(t, `{"model":"cn:glm-5.2","messages":[`+
		`{"role":"assistant","content":"a","reasoning_content":"t"}]}`)
	mirrorUpstreamReasoning(obj)
	if got, _ := obj["messages"].([]any)[0].(map[string]any)["reasoning"]; got != "t" {
		t.Errorf("带 cn: 前缀应命中，reasoning=%v", got)
	}
}

// TestMirrorUpstreamReasoningNegative 三类反例：非六家族不碰、已非空不覆盖、
// 空 reasoning_content 不造字段。
func TestMirrorUpstreamReasoningNegative(t *testing.T) {
	cases := []struct {
		name  string
		body  string
		check func(t *testing.T, msg map[string]any)
	}{
		{
			name: "gpt 家族不镜像",
			body: `{"model":"gpt-5.4","messages":[{"role":"assistant","content":"a","reasoning_content":"t"}]}`,
			check: func(t *testing.T, msg map[string]any) {
				if _, has := msg["reasoning"]; has {
					t.Errorf("gpt 不该被镜像: %v", msg["reasoning"])
				}
			},
		},
		{
			name: "qwen 家族不镜像",
			body: `{"model":"qwen3-max","messages":[{"role":"assistant","content":"a","reasoning_content":"t"}]}`,
			check: func(t *testing.T, msg map[string]any) {
				if _, has := msg["reasoning"]; has {
					t.Errorf("qwen 不该被镜像: %v", msg["reasoning"])
				}
			},
		},
		{
			name: "已非空不覆盖",
			body: `{"model":"glm-5.2","messages":[{"role":"assistant","content":"a","reasoning":"kept","reasoning_content":"other"}]}`,
			check: func(t *testing.T, msg map[string]any) {
				if got, _ := msg["reasoning"].(string); got != "kept" {
					t.Errorf("reasoning=%v want kept（不得覆盖上游自己的版本）", got)
				}
			},
		},
		{
			name: "空 reasoning_content 不造字段",
			body: `{"model":"glm-5.2","messages":[{"role":"assistant","content":"a","reasoning_content":"  "}]}`,
			check: func(t *testing.T, msg map[string]any) {
				if _, has := msg["reasoning"]; has {
					t.Errorf("空值不该新建 reasoning 字段: %v", msg["reasoning"])
				}
			},
		},
		{
			name: "user 消息不镜像",
			body: `{"model":"glm-5.2","messages":[{"role":"user","content":"a","reasoning_content":"t"}]}`,
			check: func(t *testing.T, msg map[string]any) {
				if _, has := msg["reasoning"]; has {
					t.Errorf("user 没有思考段: %v", msg["reasoning"])
				}
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			obj := decodeMessages(t, c.body)
			mirrorUpstreamReasoning(obj)
			c.check(t, obj["messages"].([]any)[0].(map[string]any))
		})
	}
}

// TestPrepareBodyFillsEmptyTextAndMirrors 管线级断言：两个纯函数确实挂在
// PrepareBodyOptWithEffortsAndDefault 上，且顺序在 repack/cleanup 之后。
func TestPrepareBodyFillsEmptyTextAndMirrors(t *testing.T) {
	body := `{"model":"glm-5.2","messages":[` +
		`{"role":"user","content":[{"type":"text","text":""}]},` +
		`{"role":"assistant","content":"a","reasoning_content":"thought"}]}`
	out := PrepareBodyOpt([]byte(body), false)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatalf("out not json: %v (%s)", err, out)
	}
	msgs := obj["messages"].([]any)
	parts := msgs[0].(map[string]any)["content"].([]any)
	if got := parts[0].(map[string]any)["text"]; got != " " {
		t.Errorf("管线未补位空 text 段: %v (%s)", got, out)
	}
	if got, _ := msgs[1].(map[string]any)["reasoning"]; got != "thought" {
		t.Errorf("管线未镜像 reasoning: %v (%s)", got, out)
	}
}

// TestPrepareBodyMirrorAfterSanitize 镜像必须发生在 sanitize **之后**：否则未净化
// 的 reasoning_content 原文会被抄进 reasoning，等于绕过脱敏。
func TestPrepareBodyMirrorAfterSanitize(t *testing.T) {
	body := `{"model":"glm-5.2","messages":[{"role":"assistant","content":"a",` +
		`"reasoning_content":"see x-anthropic-billing-header for details"}]}`
	out := PrepareBodyOpt([]byte(body), true)
	var obj map[string]any
	if err := json.Unmarshal(out, &obj); err != nil {
		t.Fatalf("out not json: %v (%s)", err, out)
	}
	msg := obj["messages"].([]any)[0].(map[string]any)
	r, _ := msg["reasoning"].(string)
	if r == "" {
		t.Fatalf("未镜像: %s", out)
	}
	if r != msg["reasoning_content"] {
		t.Errorf("reasoning 与净化后的 reasoning_content 不一致（镜像在 sanitize 之前？）: %q vs %q", r, msg["reasoning_content"])
	}
}

// decodeMessages 解析测试用 body（容忍解析失败直接 Fatal）。
func decodeMessages(t *testing.T, body string) map[string]any {
	t.Helper()
	var obj map[string]any
	if err := json.Unmarshal([]byte(body), &obj); err != nil {
		t.Fatalf("bad test body: %v (%s)", err, body)
	}
	return obj
}

// jsonString 把字符串编码成 JSON 字面量（测试拼 body 用，免手写转义）。
func jsonString(s string) string {
	b, err := json.Marshal(s)
	if err != nil {
		panic(err)
	}
	return string(b)
}
