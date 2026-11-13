package scheduler

import (
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// keepaliveTestAuth 构造一个带指定过期时间的凭证。
func keepaliveTestAuth(uid string, expiresIn time.Duration) *auth.Auth {
	return &auth.Auth{
		UID:          uid,
		AccessToken:  "at-" + uid,
		RefreshToken: "rt-" + uid,
		ExpiresAt:    time.Now().Add(expiresIn).Unix(),
	}
}

// TestNeedsRefreshWindowSemantics 锚定窗口判定的边界：auth.NeedsRefresh(within) 是
// 「将在 within 内过期」，所以
//   - 剩余 1h、窗口 7 天 → 需要刷新；
//   - 剩余 30 天、窗口 7 天 → 不需要刷新。
func TestNeedsRefreshWindowSemantics(t *testing.T) {
	window := 168 * time.Hour
	near := keepaliveTestAuth("near", time.Hour)
	if !near.NeedsRefresh(window) {
		t.Error("剩余 1h、窗口 7d 应需要刷新")
	}
	far := keepaliveTestAuth("far", 30*24*time.Hour)
	if far.NeedsRefresh(window) {
		t.Error("剩余 30d、窗口 7d 不应刷新")
	}
}

// TestNeedsRefreshNoExpiryIsTrap 无 expiry（ExpiresAt <= 0）本地恒判「需要刷新」。
//
// 这是一个已知陷阱，写进测试是为了让它显式：将来若接静态 API Key 账号（没有
// expiresAt、也不能 refresh），每轮保活都会去 refresh 一个不能 refresh 的凭证。
// 参考仓对 API Key 的 NeedsRefresh 恒 false，本地接 API Key 时必须同步这条。
func TestNeedsRefreshNoExpiryIsTrap(t *testing.T) {
	a := &auth.Auth{UID: "no-expiry", AccessToken: "at", RefreshToken: "rt"}
	if !a.NeedsRefresh(168 * time.Hour) {
		t.Fatal("当前实现：无 expiry 视为需要刷新（接 API Key 前必须处理）")
	}
}

// TestKeepaliveWindowConfigValidation 窗口配置：空回落 168h，非法/<=0 fail fast。
func TestKeepaliveWindowConfigValidation(t *testing.T) {
	// 该测试只验证 scheduler 侧的窗口字段语义（0 = 不过滤），
	// 配置校验在 internal/config 的 Normalize 里，见 config 包测试。
	s := New(Config{KeepaliveRefreshWindow: 0})
	if s.cfg.KeepaliveRefreshWindow != 0 {
		t.Fatalf("窗口 = %v，期望 0（不过滤）", s.cfg.KeepaliveRefreshWindow)
	}
}
