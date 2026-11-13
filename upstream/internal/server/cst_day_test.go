package server

import (
	"testing"
	"time"

	"workbuddy2api/internal/cst"
)

// TestNextMidnightCSTIsHostZoneIndependent 夜间降级窗口的零点必须按 CST 算。
//
// 旧实现自己 new 了一个 FixedZone，值恰好也是 +8，所以行为没错；但日界定义散在
// 各处时，改一处忘一处就是静默错误。这个测试锁住「走 cst 唯一定义」这件事。
func TestNextMidnightCSTIsHostZoneIndependent(t *testing.T) {
	// UTC 主机视角：CST 2026-11-13 23:30 = UTC 15:30。
	now := time.Date(2026, 11, 13, 15, 30, 0, 0, time.UTC)
	got := nextMidnightCST(now)
	// 期望 CST 11-14 00:00 = UTC 11-13 16:00。
	want := time.Date(2026, 11, 13, 16, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("nextMidnightCST = %s，期望 CST 11-14 00:00 (%s)", got, want)
	}
}

// TestNextMidnightCSTBoundary 刚过零点时下一个零点必须是次日。
func TestNextMidnightCSTBoundary(t *testing.T) {
	justAfter := time.Date(2026, 11, 13, 0, 0, 1, 0, cst.Loc)
	if got := nextMidnightCST(justAfter); cst.Day(got) != "2026-11-14" {
		t.Fatalf("CST 00:00:01 的下一个零点应在次日，got %q", cst.Day(got))
	}
	before := time.Date(2026, 11, 13, 23, 59, 0, 0, cst.Loc)
	if got := nextMidnightCST(before); cst.Day(got) != "2026-11-14" {
		t.Fatalf("CST 23:59 的下一个零点应在次日，got %q", cst.Day(got))
	}
}
