package upstream

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

// collectStream 跑一次 Stream，返回解析后的帧（不含 [DONE]）、[DONE] 次数与原始 body。
// 与 streamFrames 的区别：允许返回 error（截断流本就会返回 errTruncatedStream），
// 截断相关断言需要连 body 一起看。
func collectStream(t *testing.T, raw string) (frames []map[string]any, done int, body string, err error) {
	t.Helper()
	rec := httptest.NewRecorder()
	err = Stream(rec, strings.NewReader(raw))
	body = rec.Body.String()
	for _, ln := range strings.Split(body, "\n") {
		ln = strings.TrimSpace(ln)
		if strings.HasPrefix(ln, "data: [DONE]") {
			done++
			continue
		}
		if strings.HasPrefix(ln, "data: ") {
			var obj map[string]any
			if jerr := json.Unmarshal([]byte(strings.TrimPrefix(ln, "data: ")), &obj); jerr != nil {
				t.Fatalf("bad frame %q: %v", ln, jerr)
			}
			frames = append(frames, obj)
		}
	}
	return frames, done, body, err
}

// finishReasons 收集所有帧里 choices[].finish_reason 的非空字符串值。
func finishReasons(frames []map[string]any) []string {
	var out []string
	for _, fr := range frames {
		chs, _ := fr["choices"].([]any)
		for _, ci := range chs {
			c, _ := ci.(map[string]any)
			if c == nil {
				continue
			}
			if v, ok := c["finish_reason"].(string); ok && v != "" {
				out = append(out, v)
			}
		}
	}
	return out
}

// TestStreamTruncationEmitsFinishReasonLength 断流（无 finish_reason、无 [DONE]）必须
// 补一帧 finish_reason=length：只读 finish_reason 的客户端才知道这一轮是残缺的。
func TestStreamTruncationEmitsFinishReasonLength(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"object\":\"chat.completion.chunk\"," +
		"\"choices\":[{\"index\":0,\"delta\":{\"content\":\"半截回答\"},\"finish_reason\":null}]}\n\n"
	frames, done, body, err := collectStream(t, raw)
	if !IsTruncatedStreamError(err) {
		t.Fatalf("err=%v want errTruncatedStream", err)
	}
	if done != 1 {
		t.Errorf("[DONE] count=%d want 1", done)
	}

	got := finishReasons(frames)
	if len(got) != 1 {
		t.Fatalf("finish_reason 帧数=%d want 1（断流必须恰好补一帧）: %v", len(got), got)
	}
	if got[0] != TruncationFinishReason {
		t.Errorf("finish_reason=%q want %q（断流不能伪装成正常收尾）", got[0], TruncationFinishReason)
	}
	if TruncationFinishReason != "length" {
		t.Errorf("TruncationFinishReason=%q want length（responses/messages 适配器按它分流）",
			TruncationFinishReason)
	}

	// 补的帧必须是「只有 finish_reason」的收尾帧：delta 为空，不能凭空塞正文。
	for _, fr := range frames {
		chs, _ := fr["choices"].([]any)
		for _, ci := range chs {
			c, _ := ci.(map[string]any)
			if c == nil {
				continue
			}
			if c["finish_reason"] != TruncationFinishReason {
				continue
			}
			delta, _ := c["delta"].(map[string]any)
			if len(delta) != 0 {
				t.Errorf("截断终态帧的 delta 应为空，实际 %v", delta)
			}
		}
	}

	// 帧序：截断终态 → error 帧 → [DONE]。客户端顺序消费，位置错了等于没发。
	iFinish := strings.Index(body, `"finish_reason":"length"`)
	iErr := strings.Index(body, "upstream_truncated")
	iDone := strings.Index(body, "data: [DONE]")
	if iFinish < 0 || iErr < 0 || iDone < 0 {
		t.Fatalf("缺帧: finish=%d err=%d done=%d body=%q", iFinish, iErr, iDone, body)
	}
	if !(iFinish < iErr && iErr < iDone) {
		t.Errorf("帧序应为 finish_reason → error → [DONE]，实际 finish=%d err=%d done=%d\n%s",
			iFinish, iErr, iDone, body)
	}
}

// TestStreamNormalFinishNoExtraFinishFrame 正常收尾（finish_reason + [DONE]）不得多补
// 截断帧——多一个 finish_reason 会把前一个洗掉，客户端读到的终态就错了。
func TestStreamNormalFinishNoExtraFinishFrame(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"}," +
		"\"finish_reason\":null}]}\n\n" +
		"data: {\"id\":\"x1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n" +
		"data: [DONE]\n\n"
	frames, done, body, err := collectStream(t, raw)
	if err != nil {
		t.Fatalf("正常收尾不该返回错误: %v", err)
	}
	if done != 1 {
		t.Errorf("[DONE] count=%d want 1", done)
	}
	got := finishReasons(frames)
	if len(got) != 1 || got[0] != "stop" {
		t.Errorf("finish_reason=%v want [stop]（不得多补截断帧）", got)
	}
	if strings.Contains(body, "upstream_truncated") {
		t.Errorf("正常收尾不该有截断 error 帧: %q", body)
	}
	if strings.Contains(body, `"finish_reason":"length"`) {
		t.Errorf("正常收尾不该补 length 帧: %q", body)
	}
}

// TestStreamFinishSeenWithoutDoneNoExtraFrame 上游给了 finish_reason 但漏发 [DONE]：
// 已有终态，不再补 length（否则两个终态互相覆盖）。
func TestStreamFinishSeenWithoutDoneNoExtraFrame(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"choices\":[{\"index\":0,\"delta\":{\"content\":\"hi\"}," +
		"\"finish_reason\":null}]}\n\n" +
		"data: {\"id\":\"x1\",\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n"
	frames, done, body, err := collectStream(t, raw)
	if err != nil {
		t.Fatalf("已有 finish_reason 不该判截断: %v", err)
	}
	if done != 1 {
		t.Errorf("[DONE] count=%d want 1（漏发时兜底补一个）", done)
	}
	got := finishReasons(frames)
	if len(got) != 1 || got[0] != "stop" {
		t.Errorf("finish_reason=%v want [stop]", got)
	}
	if strings.Contains(body, "upstream_truncated") {
		t.Errorf("已有终态不该补截断 error 帧: %q", body)
	}
}

// TestStreamTruncationToolCallsFinish 断流且上游只发了 tool_calls 分片：终态补的是
// length（内容残缺），不能补成 tool_calls（那会让客户端去执行残缺参数）。
func TestStreamTruncationToolCallsFinish(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"choices\":[{\"index\":0,\"delta\":{\"tool_calls\":[" +
		"{\"index\":0,\"id\":\"call_1\",\"type\":\"function\",\"function\":{\"name\":\"f\",\"arguments\":\"{\\\"a\\\":\"}}]}," +
		"\"finish_reason\":null}]}\n\n"
	frames, _, _, err := collectStream(t, raw)
	if !IsTruncatedStreamError(err) {
		t.Fatalf("err=%v want errTruncatedStream", err)
	}
	got := finishReasons(frames)
	if len(got) != 1 || got[0] != TruncationFinishReason {
		t.Errorf("finish_reason=%v want [%s]", got, TruncationFinishReason)
	}
}

// TestAggregateTruncationFinishReason 非流式聚合路径同口径：断流时 finish_reason
// 必须是 length，不能是默认的 stop。
func TestAggregateTruncationFinishReason(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"model\":\"m\",\"choices\":[{\"index\":0," +
		"\"delta\":{\"content\":\"半截\"},\"finish_reason\":null}]}\n\n"
	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("Aggregate: %v", err)
	}
	chs, _ := resp["choices"].([]any)
	if len(chs) == 0 {
		t.Fatalf("响应没有 choices: %#v", resp)
	}
	c, _ := chs[0].(map[string]any)
	if got := c["finish_reason"]; got != TruncationFinishReason {
		t.Errorf("finish_reason=%v want %q（非流式断流也要标截断）", got, TruncationFinishReason)
	}
}

// TestAggregateNormalFinishUnchanged 非流式正常收尾不受影响（零回归）。
func TestAggregateNormalFinishUnchanged(t *testing.T) {
	raw := "data: {\"id\":\"x1\",\"model\":\"m\",\"choices\":[{\"index\":0," +
		"\"delta\":{\"content\":\"ok\"},\"finish_reason\":null}]}\n\n" +
		"data: {\"id\":\"x1\",\"model\":\"m\",\"choices\":[{\"index\":0," +
		"\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n" +
		"data: [DONE]\n\n"
	resp, err := Aggregate(strings.NewReader(raw))
	if err != nil {
		t.Fatalf("Aggregate: %v", err)
	}
	c := resp["choices"].([]any)[0].(map[string]any)
	if got := c["finish_reason"]; got != "stop" {
		t.Errorf("finish_reason=%v want stop", got)
	}
}
