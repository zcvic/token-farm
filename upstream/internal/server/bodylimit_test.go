package server

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

// TestReadBodyUnderLimit 正常体积的 body 原样返回。
func TestReadBodyUnderLimit(t *testing.T) {
	h := &Handler{cfg: Config{MaxPayloadBytes: 1024}}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(`{"model":"x"}`))
	body, berr := h.readBody(httptest.NewRecorder(), req)
	if berr != nil {
		t.Fatalf("readBody: %v", berr)
	}
	if string(body) != `{"model":"x"}` {
		t.Fatalf("got %q", body)
	}
}

// TestReadBodyExactlyAtLimit 恰好等于上限不算超限（limit+1 的读法必须精确）。
func TestReadBodyExactlyAtLimit(t *testing.T) {
	h := &Handler{cfg: Config{MaxPayloadBytes: 8}}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader("12345678"))
	body, berr := h.readBody(httptest.NewRecorder(), req)
	if berr != nil {
		t.Fatalf("恰好等于上限不应报错: %v", berr)
	}
	if len(body) != 8 {
		t.Fatalf("len=%d", len(body))
	}
}

// TestReadBodyOverLimit 超过一个字节即判超限。
func TestReadBodyOverLimit(t *testing.T) {
	h := &Handler{cfg: Config{MaxPayloadBytes: 8}}
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader("123456789"))
	_, berr := h.readBody(httptest.NewRecorder(), req)
	if berr == nil || !berr.TooLarge {
		t.Fatalf("期望 TooLarge，got %v", berr)
	}
	if berr.Limit != 8 {
		t.Fatalf("Limit=%d", berr.Limit)
	}
}

// TestReadBodyDefaultLimit 未配置时回落 64 MiB。
func TestReadBodyDefaultLimit(t *testing.T) {
	h := &Handler{}
	if got := h.maxPayload(); got != defaultMaxPayloadBytes {
		t.Fatalf("maxPayload=%d，期望 %d", got, defaultMaxPayloadBytes)
	}
}

// TestWriteBodyErrorTooLarge 超限必须是 413 + payload_too_large（不是 400）。
func TestWriteBodyErrorTooLarge(t *testing.T) {
	h := &Handler{}
	rec := httptest.NewRecorder()
	h.writeBodyError(rec, &bodyReadError{TooLarge: true, Limit: 8})
	if rec.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("code=%d，期望 413", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "payload_too_large") {
		t.Fatalf("body=%s", rec.Body.String())
	}
}

// TestWriteBodyErrorOther 非超限错误保持 400 invalid_request。
func TestWriteBodyErrorOther(t *testing.T) {
	h := &Handler{}
	rec := httptest.NewRecorder()
	h.writeBodyError(rec, &bodyReadError{Err: io.ErrUnexpectedEOF})
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("code=%d，期望 400", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "read body:") {
		t.Fatalf("body=%s", rec.Body.String())
	}
}

// TestReadBodyOverLimitDrains 超限后必须把剩余字节排空（否则 net/http 关连接，
// 客户端看不到 413）。用一个记录读取量的 body 验证确实读到了末尾。
func TestReadBodyOverLimitDrains(t *testing.T) {
	h := &Handler{cfg: Config{MaxPayloadBytes: 4}}
	payload := strings.Repeat("x", 1024)
	req := httptest.NewRequest(http.MethodPost, "/v1/chat/completions", strings.NewReader(payload))
	_, berr := h.readBody(httptest.NewRecorder(), req)
	if berr == nil || !berr.TooLarge {
		t.Fatalf("期望 TooLarge，got %v", berr)
	}
	// drainBody 读满 limit 字节后停；剩余未读字节数应远小于原始 1024。
	rest, _ := io.ReadAll(req.Body)
	if len(rest) >= len(payload) {
		t.Fatalf("未排空：剩余 %d 字节", len(rest))
	}
}

// TestChatNonJSONBodyRejectedLocally 入站 body 不是 JSON 时网关自己拒绝，不再原样转发。
//
// 此前这条路是透传：客户端发什么上游就收什么，于是 #295 现场只拿到上游的
// 「invalid character 'P' looking for beginning of value」（code 11101）——那句话说的
// 是「收到的 body 第一个字节是 P」，却看不出是谁发坏的。这里钉住两件事：不打上游
// （省一次真扣积分）、回包带上开头片段（报的人直接能认出自己的客户端发了什么）。
func TestChatNonJSONBodyRejectedLocally(t *testing.T) {
	called := false
	up := newFakeUpstream(t, func(string) (int, string, bool) {
		called = true
		return 200, sseOK, true
	})
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "j1", AccessToken: "at-j1", ExpiresAt: 9999999999}),
		Upstream: up,
	})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader("POST /v2/chat HTTP/1.1\nHost: example\n")))

	if rec.Code != 400 {
		t.Fatalf("code=%d want 400, body=%s", rec.Code, rec.Body)
	}
	if called {
		t.Error("畸形 body 不该打到上游（会真花积分）")
	}
	var resp struct {
		Error struct {
			Code    string `json:"code"`
			Message string `json:"message"`
		} `json:"error"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("回包不是 JSON: %v (%s)", err, rec.Body)
	}
	if resp.Error.Code != "invalid_request" {
		t.Errorf("code=%q want invalid_request", resp.Error.Code)
	}
	if !strings.Contains(resp.Error.Message, "POST /v2/chat") {
		t.Errorf("错误应带开头片段，got %q", resp.Error.Message)
	}
}

// TestChatBOMJSONStillForwarded 合法 JSON 带 BOM 时照旧转发（不误伤客户端）。
func TestChatBOMJSONStillForwarded(t *testing.T) {
	called := false
	up := newFakeUpstream(t, func(string) (int, string, bool) {
		called = true
		return 200, sseOK, true
	})
	h := NewHandler(Config{
		Pool:     testPoolWith(&auth.Auth{UID: "j1", AccessToken: "at-j1", ExpiresAt: 9999999999}),
		Upstream: up,
	})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader("\xef\xbb\xbf{\"model\":\"glm-5.2\",\"messages\":[{\"role\":\"user\",\"content\":\"hi\"}],\"stream\":true}")))
	if rec.Code != 200 {
		t.Fatalf("code=%d want 200, body=%s", rec.Code, rec.Body)
	}
	if !called {
		t.Error("合法 JSON 应照旧打到上游")
	}
}
