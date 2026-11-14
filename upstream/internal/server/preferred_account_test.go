package server

import (
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"workbuddy2api/internal/auth"
)

// preferredAccountRequestBody 一个最小可用的对话请求体。
const preferredAccountRequestBody = `{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`

// TestPreferredAccountSpendsThatAccountFirst 是本功能的核心断言：
// 带 `X-Preferred-Account` 的请求必须先落到指定账号上，即使按常规加权选号
// 它会先选到另一个积分更高的账号。
func TestPreferredAccountSpendsThatAccountFirst(t *testing.T) {
	p := testPoolWith(
		&auth.Auth{UID: "rich", AccessToken: "at-rich", ExpiresAt: 9999999999},
		&auth.Auth{UID: "pref", AccessToken: "at-pref", ExpiresAt: 9999999999},
	)
	// 常规选号（随机源恒 0）会选积分最高者；把 rich 抬高，确保没人干预时它先被选中。
	p.SetCredits("rich", 5000)
	p.SetCredits("pref", 1000)

	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		return 200, sseOK, true
	})
	h := NewHandler(Config{Pool: p, Upstream: up})

	req := httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(preferredAccountRequestBody))
	req.Header.Set(preferredAccountHeader, "pref")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != 200 {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body)
	}
	if len(used) != 1 {
		t.Fatalf("上游被调用 %d 次，want 1", len(used))
	}
	if want := "Bearer at-pref"; used[0] != want {
		t.Fatalf("请求用了 %q，want %q（优先账号应压过常规加权选号）", used[0], want)
	}
}

// TestPreferredAccountFallsBackWhenUnknown 优先账号不存在 / 不属于本池时必须
// 静默回落到普通选号，而不是让整把密钥 503 —— 「优先」不是「只走」。
func TestPreferredAccountFallsBackWhenUnknown(t *testing.T) {
	p := testPoolWith(
		&auth.Auth{UID: "a1", AccessToken: "at-a1", ExpiresAt: 9999999999},
	)
	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		return 200, sseOK, true
	})
	h := NewHandler(Config{Pool: p, Upstream: up})

	req := httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(preferredAccountRequestBody))
	req.Header.Set(preferredAccountHeader, "ghost")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != 200 {
		t.Fatalf("优先账号不存在时应回落到池子，得到 code=%d body=%s", rec.Code, rec.Body)
	}
	if len(used) != 1 || used[0] != "Bearer at-a1" {
		t.Fatalf("used=%v，want [Bearer at-a1]", used)
	}
}

// TestPreferredAccountNotRetriedAfterFailure 优先账号失败后不得反复重试：
// 同一请求内它已进 tried 集，后续轮转应换号，否则会在一个坏号上耗尽轮转次数。
func TestPreferredAccountNotRetriedAfterFailure(t *testing.T) {
	p := testPoolWith(
		&auth.Auth{UID: "bad", AccessToken: "at-bad", ExpiresAt: 9999999999},
		&auth.Auth{UID: "good", AccessToken: "at-good", ExpiresAt: 9999999999},
	)
	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		if authz == "Bearer at-bad" {
			return 401, `{"error":{"message":"unauthorized"}}`, false
		}
		return 200, sseOK, true
	})
	h := NewHandler(Config{Pool: p, Upstream: up})

	req := httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(preferredAccountRequestBody))
	req.Header.Set(preferredAccountHeader, "bad")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)

	if rec.Code != 200 {
		t.Fatalf("换号后应成功，得到 code=%d body=%s", rec.Code, rec.Body)
	}
	if len(used) != 2 {
		t.Fatalf("上游被调用 %d 次，want 2（优先号失败一次后换号）: %v", len(used), used)
	}
	if used[0] != "Bearer at-bad" || used[1] != "Bearer at-good" {
		t.Fatalf("调用顺序 %v，want [Bearer at-bad Bearer at-good]", used)
	}
}

// TestPreferredAccountHeaderValidation 只接受可见 ASCII；非法值按「未指定」
// 处理，不能把异常字节当 uid 查表，也不能让请求因此失败。
func TestPreferredAccountHeaderValidation(t *testing.T) {
	cases := []struct {
		raw  string
		want string
	}{
		{"abc123", "abc123"},
		{"  spaced  ", "spaced"},
		{"", ""},
		{strings.Repeat("x", 128), strings.Repeat("x", 128)},
		{strings.Repeat("x", 129), ""},
		{"has space", ""},
		{"中文", ""},
		{"tab\there", ""},
	}
	for _, c := range cases {
		req := httptest.NewRequest("POST", "/v1/chat/completions",
			strings.NewReader(preferredAccountRequestBody))
		req.Header.Set(preferredAccountHeader, c.raw)
		if got := preferredAccountFromRequest(req); got != c.want {
			t.Errorf("preferredAccountFromRequest(%q)=%q，want %q", c.raw, got, c.want)
		}
	}
}
