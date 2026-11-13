package upstream

import (
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

// TestSplitConcatenatedJSONBasic 两个对象首尾相接（有/无空白分隔）都要切开。
func TestSplitConcatenatedJSONBasic(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want []string
	}{
		{
			name: "空格分隔",
			in:   `{"id":"a"} {"id":"b"}`,
			want: []string{`{"id":"a"}`, `{"id":"b"}`},
		},
		{
			name: "无分隔",
			in:   `{"id":"a"}{"id":"b"}`,
			want: []string{`{"id":"a"}`, `{"id":"b"}`},
		},
		{
			name: "三个对象",
			in:   `{"a":1} {"b":2} {"c":3}`,
			want: []string{`{"a":1}`, `{"b":2}`, `{"c":3}`},
		},
		{
			name: "前后有空白",
			in:   `  {"a":1} {"b":2}  `,
			want: []string{`{"a":1}`, `{"b":2}`},
		},
		{
			name: "数组对象混排",
			in:   `[1,2] {"a":1}`,
			want: []string{`[1,2]`, `{"a":1}`},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := splitConcatenatedJSON(c.in)
			if len(got) != len(c.want) {
				t.Fatalf("拆出 %d 份，期望 %d 份: %q", len(got), len(c.want), got)
			}
			for i := range got {
				if got[i] != c.want[i] {
					t.Errorf("第 %d 份 = %q，期望 %q", i, got[i], c.want[i])
				}
				if !json.Valid([]byte(got[i])) {
					t.Errorf("第 %d 份不是合法 JSON: %q", i, got[i])
				}
			}
		})
	}
}

// TestSplitConcatenatedJSONStringBraces 字符串里的花括号 / 转义引号不算结构。
func TestSplitConcatenatedJSONStringBraces(t *testing.T) {
	in := `{"a":"}{"} {"b":"x\"y"}`
	got := splitConcatenatedJSON(in)
	if len(got) != 2 {
		t.Fatalf("字符串内花括号被误当结构，拆出 %d 份: %q", len(got), got)
	}
	for _, p := range got {
		if !json.Valid([]byte(p)) {
			t.Errorf("不是合法 JSON: %q", p)
		}
	}
}

// TestSplitConcatenatedJSONRefuse 切不动的一律返回单元素（调用方原样透传）。
func TestSplitConcatenatedJSONRefuse(t *testing.T) {
	cases := []struct {
		name string
		in   string
	}{
		{"单个对象", `{"a":1}`},
		{"括号不配平", `{"a":1`},
		{"多一个右括号", `{"a":1}}`},
		{"末尾半截", `{"a":1} {"b":`},
		{"纯文本心跳", `: keep-alive`},
		{"纯文本", `hello world`},
		{"空串", ``},
		{"只有空白", `   `},
		{"字符串未闭合", `"abc`},
		{"非法首字符", `@{"a":1}`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got := splitConcatenatedJSON(c.in)
			if len(got) != 1 {
				t.Fatalf("应当放弃拆分，却拆出 %d 份: %q", len(got), got)
			}
			if got[0] != c.in {
				t.Errorf("放弃时须原样返回，得到 %q 期望 %q", got[0], c.in)
			}
		})
	}
}

// TestSplitConcatenatedJSONCap 超过上限时整体放弃，不产出上千次写。
func TestSplitConcatenatedJSONCap(t *testing.T) {
	var sb strings.Builder
	for i := 0; i < maxSplitParts+5; i++ {
		sb.WriteString(`{"a":1} `)
	}
	got := splitConcatenatedJSON(sb.String())
	if len(got) != 1 {
		t.Fatalf("超过 maxSplitParts 应整体放弃，却拆出 %d 份", len(got))
	}
}

// TestLooksLikeJSON 只认 { / [ 开头，纯文本心跳不算。
func TestLooksLikeJSON(t *testing.T) {
	yes := []string{`{"a":1}`, `[1]`, `  {"a":1}`, "\n[1,2]"}
	for _, s := range yes {
		if !looksLikeJSON(s) {
			t.Errorf("%q 应判定为「看起来是 JSON」", s)
		}
	}
	no := []string{`: ping`, `hello`, ``, `   `, `"str"`, `123`}
	for _, s := range no {
		if looksLikeJSON(s) {
			t.Errorf("%q 不应判定为 JSON", s)
		}
	}
}

// TestStreamHintSplitsGluedFrame 端到端：上游把一个 data 行塞两个对象，
// 客户端侧必须看到「每帧单个合法 JSON、内容不丢、[DONE] 恰好一个」。
func TestStreamHintSplitsGluedFrame(t *testing.T) {
	glued := strings.Join([]string{
		`data: {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]} {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}`,
		"",
		`data: {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"世界"},"finish_reason":null}]}`,
		"",
		`data: {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")

	var sink strings.Builder
	w := &flushRecorder{buf: &sink}
	if err := StreamHint(w, strings.NewReader(glued), nil); err != nil {
		t.Fatalf("StreamHint 返回错误: %v", err)
	}

	var (
		events []string
		doneN  int
		text   strings.Builder
	)
	for _, block := range strings.Split(sink.String(), "\n\n") {
		block = strings.TrimSpace(block)
		if block == "" {
			continue
		}
		// 每个事件必须恰好一行 data（拆分后不得再出现一个事件两行 data）。
		lines := strings.Split(block, "\n")
		var dataLines []string
		for _, l := range lines {
			if strings.HasPrefix(l, "data: ") {
				dataLines = append(dataLines, strings.TrimPrefix(l, "data: "))
			}
		}
		if len(dataLines) != 1 {
			t.Fatalf("事件里有 %d 行 data，期望恰好 1 行: %q", len(dataLines), block)
		}
		p := dataLines[0]
		if p == "[DONE]" {
			doneN++
			continue
		}
		if !json.Valid([]byte(p)) {
			t.Fatalf("客户端收到非法 JSON 帧: %q", p)
		}
		events = append(events, p)
		var obj map[string]any
		if err := json.Unmarshal([]byte(p), &obj); err != nil {
			t.Fatalf("解析失败: %v", err)
		}
		if chs, ok := obj["choices"].([]any); ok {
			for _, ci := range chs {
				c, _ := ci.(map[string]any)
				if c == nil {
					continue
				}
				if d, ok := c["delta"].(map[string]any); ok {
					if v, ok := d["content"].(string); ok {
						text.WriteString(v)
					}
				}
			}
		}
	}
	if doneN != 1 {
		t.Errorf("[DONE] 出现 %d 次，期望恰好 1 次", doneN)
	}
	if got := text.String(); got != "你好世界" {
		t.Errorf("正文 = %q，期望 %q（拆分不得丢内容）", got, "你好世界")
	}
	if len(events) != 4 {
		t.Errorf("拆开后应有 4 个内容事件，实际 %d 个", len(events))
	}
}

// TestStreamHintKeepsPlainTextPassthrough 非 JSON 行原样透传，不被拆分逻辑误伤。
func TestStreamHintKeepsPlainTextPassthrough(t *testing.T) {
	raw := strings.Join([]string{
		`: keep-alive`,
		"",
		`data: {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"ok"},"finish_reason":null}]}`,
		"",
		`data: {"id":"x1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")
	var sink strings.Builder
	w := &flushRecorder{buf: &sink}
	if err := StreamHint(w, strings.NewReader(raw), nil); err != nil {
		t.Fatalf("StreamHint 返回错误: %v", err)
	}
	if !strings.Contains(sink.String(), ": keep-alive") {
		t.Error("注释行应原样透传")
	}
}

// TestAggregateSplitsGluedFrame 非流式聚合路径同样要拆，否则整段内容丢失。
func TestAggregateSplitsGluedFrame(t *testing.T) {
	raw := strings.Join([]string{
		`data: {"id":"x1","model":"m","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]} {"id":"x1","model":"m","choices":[{"index":0,"delta":{"content":"前半"},"finish_reason":null}]}`,
		"",
		`data: {"id":"x1","model":"m","choices":[{"index":0,"delta":{"content":"后半"},"finish_reason":null}]}`,
		"",
		`data: {"id":"x1","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}`,
		"",
		"data: [DONE]",
		"",
	}, "\n")

	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("Aggregate 返回错误: %v", err)
	}
	chs, ok := resp["choices"].([]any)
	if !ok || len(chs) == 0 {
		t.Fatalf("响应没有 choices: %#v", resp)
	}
	msg, _ := chs[0].(map[string]any)["message"].(map[string]any)
	if msg == nil {
		t.Fatalf("choices[0] 没有 message: %#v", chs[0])
	}
	if got := msg["content"]; got != "前半后半" {
		t.Errorf("content = %q，期望 %q（粘帧里的内容不得丢失）", got, "前半后半")
	}
	if got := chs[0].(map[string]any)["finish_reason"]; got != "stop" {
		t.Errorf("finish_reason = %v，期望 stop", got)
	}
}

// flushRecorder 最小 http.ResponseWriter + Flusher 实现。
type flushRecorder struct {
	buf       *strings.Builder
	header    http.Header
	flushed   int
	status    int
	wroteHead bool
}

func (f *flushRecorder) Header() http.Header {
	if f.header == nil {
		f.header = http.Header{}
	}
	return f.header
}

func (f *flushRecorder) Write(p []byte) (int, error) {
	f.wroteHead = true
	return f.buf.Write(p)
}

func (f *flushRecorder) WriteHeader(status int) {
	f.status = status
	f.wroteHead = true
}

func (f *flushRecorder) Flush() { f.flushed++ }
