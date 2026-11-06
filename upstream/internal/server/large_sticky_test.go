package server

import (
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/session"
)

// largeRequestBody 构造一个超过 LargeRequestBytes 的合法请求体。
func largeRequestBody(convID string) string {
	return `{"model":"glm-5.2","messages":[{"role":"user","content":"` +
		strings.Repeat("x", 2048) + `"}],"metadata":{"conversation_id":"` + convID + `"}}`
}

// TestLargeRequestStickyModeKeepsCacheAffinity 是本次修复的核心断言：
// sticky（默认）模式下，同一会话的连续大请求必须落到**同一个账号**，并且成功
// 后要写回绑定。否则每轮都换号，上游 prompt cache（按账号隔离）永远命不中。
//
// 0044 旧行为（rotate）会 unbind + 逐请求排除，因此本用例在旧代码上会失败。
func TestLargeRequestStickyModeKeepsCacheAffinity(t *testing.T) {
	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"a1", "a2"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "a1", AccessToken: "at-a1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "a2", AccessToken: "at-a2", ExpiresAt: 9999999999},
	)
	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		return 200, sseOK, true
	})
	h := NewHandler(Config{
		Pool:                 p,
		Upstream:             up,
		Session:              sess,
		SoftCooldown:         time.Minute,
		LargeRequestBytes:    1024,
		LargeRequestCooldown: 90 * time.Second,
		// LargeRequestMode 留空 = 默认 sticky
	})

	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("POST", "/v1/chat/completions",
			strings.NewReader(largeRequestBody("conv-large-sticky")))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("第 %d 次请求 code=%d body=%s", i+1, rec.Code, rec.Body)
		}
	}

	if len(used) != 2 {
		t.Fatalf("上游被调用 %d 次，want 2", len(used))
	}
	if used[0] != used[1] {
		t.Fatalf("同一会话的大请求换了账号：%q → %q（sticky 模式应固定同号以保住 prompt cache）",
			used[0], used[1])
	}
	uid, ok := st.lastUID("conv-large-sticky")
	if !ok {
		t.Fatalf("大请求成功后应写回粘性绑定，binds=%v", st.binds)
	}
	if want := "Bearer at-" + uid; used[0] != want {
		t.Fatalf("绑定号 %q 与实际使用的账号 %q 不一致", uid, used[0])
	}
}

// TestLargeRequestRotateModeStillRotates 保留 0044 旧行为的逃生门：显式配置
// large_request_mode=rotate 时仍然逐请求换号、不写绑定。这样上游风控极严的
// 部署可以按需切回，而不是被默认值一刀切。
func TestLargeRequestRotateModeStillRotates(t *testing.T) {
	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"a1", "a2"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "a1", AccessToken: "at-a1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "a2", AccessToken: "at-a2", ExpiresAt: 9999999999},
	)
	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		return 200, sseOK, true
	})
	h := NewHandler(Config{
		Pool:                 p,
		Upstream:             up,
		Session:              sess,
		SoftCooldown:         time.Minute,
		LargeRequestBytes:    1024,
		LargeRequestCooldown: 90 * time.Second,
		LargeRequestMode:     "rotate",
	})

	for i := 0; i < 2; i++ {
		req := httptest.NewRequest("POST", "/v1/chat/completions",
			strings.NewReader(largeRequestBody("conv-large-rotate")))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("第 %d 次请求 code=%d body=%s", i+1, rec.Code, rec.Body)
		}
	}

	if len(used) != 2 {
		t.Fatalf("上游被调用 %d 次，want 2", len(used))
	}
	if used[0] == used[1] {
		t.Fatalf("rotate 模式应逐请求换号，两次都用了 %q", used[0])
	}
	if uid, ok := st.lastUID("conv-large-rotate"); ok {
		t.Fatalf("rotate 模式不应写粘性绑定，got %q（binds=%v）", uid, st.binds)
	}
}

// TestLargeRequestStickyModeSpreadsFirstBind 首绑仍需分散：两个不同会话的
// 第一条大请求应尽量落到不同账号（靠 large_request_cooldown 排除窗口），
// 而不是全部挤在同一个号上。注意与上面的「同会话固定同号」并不矛盾。
func TestLargeRequestStickyModeSpreadsFirstBind(t *testing.T) {
	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"a1", "a2"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "a1", AccessToken: "at-a1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "a2", AccessToken: "at-a2", ExpiresAt: 9999999999},
	)
	var mu sync.Mutex
	var used []string
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		mu.Lock()
		used = append(used, authz)
		mu.Unlock()
		return 200, sseOK, true
	})
	h := NewHandler(Config{
		Pool:                 p,
		Upstream:             up,
		Session:              sess,
		SoftCooldown:         time.Minute,
		LargeRequestBytes:    1024,
		LargeRequestCooldown: 90 * time.Second,
	})

	for _, conv := range []string{"conv-large-a", "conv-large-b"} {
		req := httptest.NewRequest("POST", "/v1/chat/completions",
			strings.NewReader(largeRequestBody(conv)))
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code != 200 {
			t.Fatalf("会话 %s code=%d body=%s", conv, rec.Code, rec.Body)
		}
	}

	if len(used) != 2 {
		t.Fatalf("上游被调用 %d 次，want 2", len(used))
	}
	if used[0] == used[1] {
		t.Fatalf("两个新会话的首条大请求应分散到不同账号，实际都是 %q", used[0])
	}
}
