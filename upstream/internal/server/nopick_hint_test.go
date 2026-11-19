// 选不到号时的 503 必须自带成因拆解（移植自 panel d40d4b8，issue #276）。
package server

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

// TestNoHealthyAccountHintCarriesNoPick 池里两个号都在、都健康，但都被面板手动停用，
// 选号必失败。回给客户端的 gateway_hint 必须说清是 paused 挡的：此前只有一句
// 「no healthy account available in pool; check /status or retry later」，而 /status
// 上这两个号可能看起来完全正常。
func TestNoHealthyAccountHintCarriesNoPick(t *testing.T) {
	calls := 0
	up := newFakeUpstream(t, func(string) (int, string, bool) {
		calls++
		return 200, `{}`, false
	})
	p := testPoolWith(
		&auth.Auth{UID: "u1", AccessToken: "at-1", ExpiresAt: 9999999999},
		&auth.Auth{UID: "u2", AccessToken: "at-2", ExpiresAt: 9999999999},
	)
	p.SetManualDisabled("u1", true, "test")
	p.SetManualDisabled("u2", true, "test")
	h := NewHandler(Config{Pool: p, Upstream: up})

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions",
		strings.NewReader(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`)))

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("code=%d body=%s (want 503)", rec.Code, rec.Body)
	}
	if calls != 0 {
		t.Fatalf("选号失败不该打上游，calls=%d", calls)
	}
	body := rec.Body.String()
	for _, want := range []string{"no_healthy_account", "no_pick{", "paused=2", "total=2"} {
		if !strings.Contains(body, want) {
			t.Errorf("503 文案缺少 %q: %s", want, body)
		}
	}
}
