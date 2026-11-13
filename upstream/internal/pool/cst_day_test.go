package pool

import (
	"testing"
	"time"

	"workbuddy2api/internal/cst"
)

// TestNextDay4AMIsCST 硬冷却的「次日 04:00」必须按 CST 算，与宿主时区无关。
//
// 这是 P0-4 最容易漏的一处：它不在原清单的 8 行表里，但和 scheduler 的日界是
// 同一类问题。宿主是 UTC 时，旧实现算出的「04:00」落在 CST 12:00——签到早已结束，
// 硬冷却白等半天，而且不报错。
func TestNextDay4AMIsCST(t *testing.T) {
	// UTC 主机视角：CST 2026-11-13 02:00 = UTC 2026-11-12 18:00。
	now := time.Date(2026, 11, 12, 18, 0, 0, 0, time.UTC)
	got := nextDay4AM(now)
	// 期望当天（CST 11-13）04:00 = UTC 11-12 20:00。
	want := time.Date(2026, 11, 12, 20, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("nextDay4AM = %s，期望 CST 11-13 04:00 (%s)", got, want)
	}
}

// TestNextDay4AMBeforeAndAfterBoundary 04:00 之前取当天、04:00 及之后取次日。
func TestNextDay4AMBeforeAndAfterBoundary(t *testing.T) {
	early := time.Date(2026, 11, 13, 3, 0, 0, 0, cst.Loc)
	if got := nextDay4AM(early); cst.Day(got) != "2026-11-13" {
		t.Fatalf("CST 03:00 的下一个 04:00 应在当天，got %q", cst.Day(got))
	}
	exact := time.Date(2026, 11, 13, 4, 0, 0, 0, cst.Loc)
	if got := nextDay4AM(exact); cst.Day(got) != "2026-11-14" {
		t.Fatalf("CST 04:00 整的下一个 04:00 应在次日，got %q", cst.Day(got))
	}
	late := time.Date(2026, 11, 13, 5, 0, 0, 0, cst.Loc)
	if got := nextDay4AM(late); cst.Day(got) != "2026-11-14" {
		t.Fatalf("CST 05:00 的下一个 04:00 应在次日，got %q", cst.Day(got))
	}
}

// TestNextDay4AMMonthRollover 月末/年末进位不能靠手算 day+1。
func TestNextDay4AMMonthRollover(t *testing.T) {
	t0 := time.Date(2026, 11, 30, 5, 0, 0, 0, cst.Loc)
	if got := nextDay4AM(t0); cst.Day(got) != "2026-12-01" {
		t.Fatalf("跨月 got %q", cst.Day(got))
	}
	t1 := time.Date(2026, 12, 31, 5, 0, 0, 0, cst.Loc)
	if got := nextDay4AM(t1); cst.Day(got) != "2027-01-01" {
		t.Fatalf("跨年 got %q", cst.Day(got))
	}
}
