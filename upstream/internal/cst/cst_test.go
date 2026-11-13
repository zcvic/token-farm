package cst

import (
	"testing"
	"time"
)

// TestLocIsUTC8 固定偏移必须是 +08:00，与宿主时区无关。
func TestLocIsUTC8(t *testing.T) {
	_, off := time.Now().In(Loc).Zone()
	if off != 8*60*60 {
		t.Fatalf("偏移 %d 秒，期望 28800", off)
	}
}

// TestDayIsCSTRegardlessOfHostZone 核心断言：同一绝对时刻，在任何宿主时区下
// Day() 都必须给出同一个 CST 日期。
func TestDayIsCSTRegardlessOfHostZone(t *testing.T) {
	// 2026-11-13T17:30:00Z = CST 2026-11-14 01:30（UTC 还是 13 号，CST 已是 14 号）。
	instant := time.Date(2026, 11, 13, 17, 30, 0, 0, time.UTC)
	if got := Day(instant); got != "2026-11-14" {
		t.Fatalf("Day=%q，期望 2026-11-14（CST 已跨日）", got)
	}
	// 换一个宿主时区表示同一时刻，结果必须一致。
	la, err := time.LoadLocation("America/Los_Angeles")
	if err != nil {
		t.Skip("无 tzdata，跳过跨时区断言")
	}
	if got := Day(instant.In(la)); got != "2026-11-14" {
		t.Fatalf("宿主时区下的 Day=%q，期望 2026-11-14", got)
	}
}

// TestDayBoundary 日界必须落在 CST 00:00，而不是宿主 00:00。
func TestDayBoundary(t *testing.T) {
	// CST 2026-11-13 23:59:59
	before := time.Date(2026, 11, 13, 15, 59, 59, 0, time.UTC)
	// CST 2026-11-14 00:00:00
	after := time.Date(2026, 11, 13, 16, 0, 0, 0, time.UTC)
	if Day(before) != "2026-11-13" {
		t.Errorf("CST 23:59:59 应属 13 号，got %q", Day(before))
	}
	if Day(after) != "2026-11-14" {
		t.Errorf("CST 00:00:00 应属 14 号，got %q", Day(after))
	}
}

// TestAt 构造出的时刻必须是 CST 当天的指定钟点。
func TestAt(t *testing.T) {
	instant := time.Date(2026, 11, 13, 17, 30, 0, 0, time.UTC) // CST 11-14 01:30
	at := At(instant, 10, 0, 0)
	if at.Hour() != 10 || at.Minute() != 0 {
		t.Fatalf("At=%v", at)
	}
	if Day(at) != "2026-11-14" {
		t.Fatalf("At 的日键 %q，期望 2026-11-14", Day(at))
	}
	if _, off := at.Zone(); off != 8*60*60 {
		t.Fatalf("At 的偏移 %d", off)
	}
}

// TestNextAt 严格晚于 t：t 在钟点之前取当天，之后取次日。
func TestNextAt(t *testing.T) {
	// CST 02:00 → 下一个 04:00 是当天。
	early := time.Date(2026, 11, 13, 2, 0, 0, 0, Loc)
	if got := NextAt(early, 4, 0, 0); Day(got) != "2026-11-13" || got.Hour() != 4 {
		t.Fatalf("02:00 的下一个 04:00 = %v，期望当天 04:00", got)
	}
	// CST 04:00 整 → 下一个是次日（严格晚于）。
	exact := time.Date(2026, 11, 13, 4, 0, 0, 0, Loc)
	if got := NextAt(exact, 4, 0, 0); Day(got) != "2026-11-14" || got.Hour() != 4 {
		t.Fatalf("04:00 整的下一个 04:00 = %v，期望次日 04:00", got)
	}
	// CST 05:00 → 次日。
	late := time.Date(2026, 11, 13, 5, 0, 0, 0, Loc)
	if got := NextAt(late, 4, 0, 0); Day(got) != "2026-11-14" {
		t.Fatalf("05:00 的下一个 04:00 = %v，期望次日", got)
	}
}

// TestNextAtMonthRollover 月末进位不能靠手算 day+1。
func TestNextAtMonthRollover(t *testing.T) {
	// CST 2026-11-30 05:00 → 次日是 12-01。
	t0 := time.Date(2026, 11, 30, 5, 0, 0, 0, Loc)
	if got := NextAt(t0, 4, 0, 0); Day(got) != "2026-12-01" {
		t.Fatalf("跨月 got %q", Day(got))
	}
	// CST 2026-12-31 05:00 → 次年 01-01。
	t1 := time.Date(2026, 12, 31, 5, 0, 0, 0, Loc)
	if got := NextAt(t1, 4, 0, 0); Day(got) != "2027-01-01" {
		t.Fatalf("跨年 got %q", Day(got))
	}
}
