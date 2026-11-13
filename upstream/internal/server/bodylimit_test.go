package server

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
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
