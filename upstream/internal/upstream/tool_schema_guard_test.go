package upstream

import (
	"strings"
	"testing"
)

// 上游工具定义校验的三处收口：
//  1. 11152 工具名不合规/重名 必须归 ErrBadParams，否则落 ErrClient → 喂连败 + 轮转全池；
//  2. 关键字取值类型错（minimum:"1" 等）出站前纠偏，别指望上游宽容
//     （见 schema_normalize_test.go）；
//  3. ErrBadParams 的 gateway_hint 要指出问题在 tools，否则客户端只看到「工具定义不合规」。
func TestClassify11152ToolName(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want ErrKind
	}{
		{"紧凑 code", `{"code":11152,"msg":"the tool name is invalid or duplicated"}`, ErrBadParams},
		{"code 带空格", `{"code": 11152, "msg":"x"}`, ErrBadParams},
		{"code 字符串", `{"code":"11152","msg":"x"}`, ErrBadParams},
		{"嵌套在 extError", `{"extError":{"code":"invalid_request_error"},"code":11152}`, ErrBadParams},
		{"相邻 code 不误伤", `{"code":111520,"msg":"x"}`, ErrClient},
	} {
		if got := Classify(400, tc.body); got != tc.want {
			t.Errorf("%s: Classify(400, %s) = %v, want %v", tc.name, tc.body, got, tc.want)
		}
	}
	if got := FrameKind(`{"error":{"message":"x","code":11152}}`); got != ErrBadParams {
		t.Errorf("FrameKind(11152 frame) = %v, want ErrBadParams", got)
	}
}

func TestClassify11129ToolSchema(t *testing.T) {
	for _, tc := range []struct {
		name string
		body string
		want ErrKind
	}{
		{"紧凑 code", `{"code":11129,"msg":"invalid function call parameters"}`, ErrBadParams},
		{"code 带空格", `{"code": 11129, "msg":"x"}`, ErrBadParams},
		{"只有文案", `{"msg":"invalid function call parameters"}`, ErrBadParams},
		{"下划线形态", `{"msg":"invalid_function_call_parameters"}`, ErrBadParams},
		{"中文 displayMsg", `{"msg":"工具定义不合规"}`, ErrBadParams},
		{"相邻 code 不误伤", `{"code":111290,"msg":"x"}`, ErrClient},
	} {
		if got := Classify(400, tc.body); got != tc.want {
			t.Errorf("%s: Classify(400, %s) = %v, want %v", tc.name, tc.body, got, tc.want)
		}
	}
	// 帧里的 code 在 error.code 上、不在 message 里，只拿 message 判会漏。
	if got := FrameKind(`{"error":{"message":"x","code":11129}}`); got != ErrBadParams {
		t.Errorf("FrameKind(11129 frame) = %v, want ErrBadParams", got)
	}
}

func TestGatewayHintToolDefinition(t *testing.T) {
	for _, body := range []string{
		`{"code":11129,"msg":"invalid function call parameters"}`,
		`{"code":11152,"msg":"the tool name is invalid or duplicated"}`,
	} {
		got := GatewayHint(ErrBadParams, body, HintContext{})
		if !strings.Contains(got, "tool definition") {
			t.Errorf("%s: hint = %q, want one naming tool definitions", body, got)
		}
	}
	// 11101 请求体 JSON 畸形与工具无关，不能指向 tools（客户端会去翻自己的 schema）。
	if got := GatewayHint(ErrBadParams, `{"code":11101,"msg":"Unmarshal chat params failed"}`, HintContext{}); got != "" {
		t.Errorf("11101 hint = %q, want empty", got)
	}
}
