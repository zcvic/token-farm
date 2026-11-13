package scheduler

import (
	"testing"
	"time"

	"workbuddy2api/internal/cst"
)

// TestScheduleUsesCSTNotHostZone 排程钟点必须按 CST 解释，与宿主时区无关。
//
// 这是 P0-4 的核心回归：网关可能跑在 UTC 主机（Docker / 海外 VPS）上，此时若
// 用 now.Location() 构造槽位，配的 [10] 实际会在 CST 18:00 跑，签到早已结束。
// 断言方式：把同一个绝对时刻用不同时区表示，算出的槽位必须相等。
func TestScheduleUsesCSTNotHostZone(t *testing.T) {
	s := jitterScheduler(0)
	// 2026-11-13T01:00:00Z = CST 2026-11-13 09:00，即「CST 09:00 前半小时」。
	utcNow := time.Date(2026, 11, 13, 0, 30, 0, 0, time.UTC)

	utcAt, _ := s.nextWake(utcNow)
	// 同一绝对时刻，用 CST 与用 UTC 表示，结果必须一致。
	cstAt, _ := s.nextWake(utcNow.In(cst.Loc))
	if !utcAt.Equal(cstAt) {
		t.Fatalf("宿主时区影响结果: utc=%s cst=%s", utcAt, cstAt)
	}
	// 且必须落在 CST 09:00（= UTC 01:00）。
	want := time.Date(2026, 11, 13, 1, 0, 0, 0, time.UTC)
	if !utcAt.Equal(want) {
		t.Fatalf("槽位 = %s（%s），期望 CST 09:00 = UTC 01:00", utcAt, cst.Day(utcAt))
	}
}

// TestNextFireIsCST 直接锚定 nextFire 的日界：UTC 主机上配 hours=[10]，
// 触发的绝对时刻必须是 CST 10:00（UTC 02:00），不是 UTC 10:00。
func TestNextFireIsCST(t *testing.T) {
	now := time.Date(2026, 11, 13, 0, 0, 0, 0, time.UTC) // CST 08:00
	got := nextFire(now, []int{10})
	want := time.Date(2026, 11, 13, 2, 0, 0, 0, time.UTC) // CST 10:00
	if !got.Equal(want) {
		t.Fatalf("nextFire = %s，期望 CST 10:00 (%s)", got, want)
	}
	if cst.Day(got) != "2026-11-13" {
		t.Fatalf("日键 = %q", cst.Day(got))
	}
}

// TestDueSlotsDayBoundaryIsCST 跨 CST 零点时 dueSlots 必须把两个槽位算到不同日期。
//
// 场景：UTC 主机，cursor 在 CST 11-13 23:59，now 在 CST 11-14 01:00。
// 若日界跟宿主时区（UTC），这两天会被当成同一天，11-14 凌晨的槽位整个丢掉。
func TestDueSlotsDayBoundaryIsCST(t *testing.T) {
	s := New(Config{
		CheckinHours: []int{1}, TravelDisabled: true, ActivityDisabled: true,
		KeepaliveDisabled: true, SchoolDisabled: true, CatDisabled: true,
	})
	cursor := time.Date(2026, 11, 13, 23, 59, 0, 0, cst.Loc)
	now := time.Date(2026, 11, 14, 1, 30, 0, 0, cst.Loc)
	slots := s.dueSlots(cursor, now)
	if len(slots) != 1 {
		t.Fatalf("期望命中 1 个槽位（CST 11-14 01:00），got %d: %+v", len(slots), slots)
	}
	if cst.Day(slots[0].planned) != "2026-11-14" {
		t.Fatalf("槽位日键 = %q，期望 2026-11-14", cst.Day(slots[0].planned))
	}
}
