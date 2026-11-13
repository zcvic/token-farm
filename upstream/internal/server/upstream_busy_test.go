package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/session"
)

// TestChatProviderUnavailableDoesNotRotateOrUnbind 上游 500 + code 11134
// 「the model provider is temporarily unavailable」是供应商侧过载（issue #188：
// 15 分钟内跨 6 个不同账号同时返回），与账号健康无关。四件事必须同时成立：
//   - 不罚号（不冷却/不熔断/不喂连败）——否则高峰期把整片健康号打进 30m 冷却；
//   - body 的 retry_policy.retry_after 当 Retry-After 上报（8s），且**不换号**——
//     换号撞同一个 provider，白耗在途名额；
//   - 会话粘性绑定保留——上游前缀缓存按账号隔离，解绑后重试落到别的号等于从零
//     重建缓存（同号 97–99% vs 换号 0–12%）；
//   - 上游原文（含 requestId）透传，客户端能拿去排查。
func TestChatProviderUnavailableDoesNotRotateOrUnbind(t *testing.T) {
	const upstreamBody = `{"code":11134,"msg":"the model provider is temporarily unavailable, please retry later or switch to another model","requestId":"01a1b4d1b3f45f4b99c406eb04d68909","retry_policy":{"retry":true,"retry_after":8}}`
	calls := map[string]int{}
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		calls[authz]++
		return 500, upstreamBody, false
	})
	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"deep1", "deep2"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "deep1", AccessToken: "at-deep1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "deep2", AccessToken: "at-deep2", ExpiresAt: 9999999999},
	)
	h := NewHandler(Config{Pool: p, Upstream: up, Session: sess, SoftCooldown: time.Minute})
	sess.Bind("conv-9", "deep1")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","messages":[],"metadata":{"conversation_id":"conv-9"}}`)))

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("code=%d body=%s (want 429 + Retry-After)", rec.Code, rec.Body)
	}
	if ra := rec.Header().Get("Retry-After"); ra != "8" {
		t.Errorf("Retry-After=%q want 8（retry_policy.retry_after，非本地兜底 5s）", ra)
	}
	if !strings.Contains(rec.Body.String(), "01a1b4d1b3f45f4b99c406eb04d68909") {
		t.Errorf("429 body 应透传上游原文（含 requestId）: %s", rec.Body)
	}
	if !strings.Contains(rec.Body.String(), "upstream_busy") {
		t.Errorf("业务码应为 upstream_busy（与排队同族）: %s", rec.Body)
	}
	if calls["Bearer at-deep1"] != 1 || calls["Bearer at-deep2"] != 0 {
		t.Errorf("calls=%v want 只打粘性号一次（retry_after 在案时不轮转）", calls)
	}
	if uid, ok := st.lastUID("conv-9"); !ok || uid != "deep1" {
		t.Errorf("会话绑应保留 deep1（换号会丢账号级前缀缓存），got %s ok=%v", uid, ok)
	}
	s, _ := p.Status("deep1")
	if s.Cooling || s.Disabled || s.ErrTotal != 0 || s.BreakerFails != 0 || s.SoftStreak != 0 {
		t.Errorf("11134 是供应商过载，不得罚号: %+v", s)
	}
}

// TestChatQueueWaitingRetryAfterFromBody 6020 排队信封的等待口径来自 body
// （data.retry_after），同样不换号、不罚号、保留绑定，末端 429 + Retry-After。
func TestChatQueueWaitingRetryAfterFromBody(t *testing.T) {
	const upstreamBody = `{"code":6020,"msg":"queue.waiting.title","requestId":"01a181ca3b67934d01c3f64599c8490c","data":{"queue_position":2502,"queue_size":"999+","retry_after":40,"estimated_wait":12510}}`
	calls := map[string]int{}
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		calls[authz]++
		return 429, upstreamBody, false
	})
	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"q1", "q2"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "q1", AccessToken: "at-q1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "q2", AccessToken: "at-q2", ExpiresAt: 9999999999},
	)
	h := NewHandler(Config{Pool: p, Upstream: up, Session: sess, SoftCooldown: time.Minute})
	sess.Bind("conv-q", "q1")

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"kimi-k3.1-pro","messages":[],"metadata":{"conversation_id":"conv-q"}}`)))

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("code=%d body=%s (want 429)", rec.Code, rec.Body)
	}
	if ra := rec.Header().Get("Retry-After"); ra != "40" {
		t.Errorf("Retry-After=%q want 40（data.retry_after）", ra)
	}
	if calls["Bearer at-q1"] != 1 || calls["Bearer at-q2"] != 0 {
		t.Errorf("calls=%v want 只打粘性号一次", calls)
	}
	if uid, ok := st.lastUID("conv-q"); !ok || uid != "q1" {
		t.Errorf("排队不该解绑会话，got %s ok=%v", uid, ok)
	}
	s, _ := p.Status("q1")
	if s.Cooling || s.Disabled || s.ErrTotal != 0 || s.BreakerFails != 0 || s.SoftStreak != 0 {
		t.Errorf("6020 是全局队列信号，不得罚号: %+v", s)
	}
}

// TestChatQueueFullRotatesWithoutRetryAfter 6021（queue.full，无 retry_after）是
// 间歇空窗：保留轮转（换号有机会碰上），但**仍不罚号**，末端仍是 429 + 兜底 5s。
func TestChatQueueFullRotatesWithoutRetryAfter(t *testing.T) {
	const upstreamBody = `{"code":6021,"msg":"queue.full.title","requestId":"b1bc87ea"}`
	calls := map[string]int{}
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		calls[authz]++
		return 429, upstreamBody, false
	})
	p := testPoolWith(
		&auth.Auth{UID: "f1", AccessToken: "at-f1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "f2", AccessToken: "at-f2", ExpiresAt: 9999999999},
	)
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: time.Minute})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"kimi-k3.1-pro","messages":[]}`)))

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("code=%d body=%s (want 429)", rec.Code, rec.Body)
	}
	if ra := rec.Header().Get("Retry-After"); ra != "5" {
		t.Errorf("Retry-After=%q want 5（上游无口径时的本地兜底）", ra)
	}
	if calls["Bearer at-f1"]+calls["Bearer at-f2"] < 2 {
		t.Errorf("6021 无等待口径时应继续轮转，calls=%v", calls)
	}
	for _, uid := range []string{"f1", "f2"} {
		s, _ := p.Status(uid)
		if s.Cooling || s.Disabled || s.ErrTotal != 0 || s.BreakerFails != 0 {
			t.Errorf("%s 不得因队列满被罚: %+v", uid, s)
		}
	}
}

// TestChatProviderUnavailableWithoutRetryAfterRotates 11134 但 body 没给
// retry_policy.retry_after：仍保留轮转（间歇空窗），末端兜底 5s。
func TestChatProviderUnavailableWithoutRetryAfterRotates(t *testing.T) {
	const upstreamBody = `{"code":11134,"msg":"the model provider is temporarily unavailable"}`
	calls := map[string]int{}
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		calls[authz]++
		return 503, upstreamBody, false
	})
	p := testPoolWith(
		&auth.Auth{UID: "p1", AccessToken: "at-p1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "p2", AccessToken: "at-p2", ExpiresAt: 9999999999},
	)
	h := NewHandler(Config{Pool: p, Upstream: up, SoftCooldown: time.Minute})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","messages":[]}`)))

	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("code=%d body=%s (want 429)", rec.Code, rec.Body)
	}
	if ra := rec.Header().Get("Retry-After"); ra != "5" {
		t.Errorf("Retry-After=%q want 5（无口径兜底）", ra)
	}
	if calls["Bearer at-p1"]+calls["Bearer at-p2"] < 2 {
		t.Errorf("无 retry_after 时应继续轮转，calls=%v", calls)
	}
	for _, uid := range []string{"p1", "p2"} {
		s, _ := p.Status(uid)
		if s.Cooling || s.Disabled || s.ErrTotal != 0 || s.BreakerFails != 0 {
			t.Errorf("%s 不得因供应商过载被罚: %+v", uid, s)
		}
	}
}
