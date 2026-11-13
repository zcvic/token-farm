package pool

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/cst"
)

// TestNoteCheckinDoneToday 记账口径：签到成功后 CheckinDoneToday 为真；未知 uid
// 恒 false（不 panic）。
func TestNoteCheckinDoneToday(t *testing.T) {
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})
	if p.CheckinDoneToday("u1") {
		t.Fatal("新账号不该显示已签到")
	}
	p.NoteCheckinDone("u1")
	if !p.CheckinDoneToday("u1") {
		t.Error("NoteCheckinDone 后应为已签到")
	}
	// 未知 uid：不 panic、返回 false。
	p.NoteCheckinDone("nope")
	if p.CheckinDoneToday("nope") {
		t.Error("未知 uid 不该显示已签到")
	}
}

// TestCheckinDoneTodayUsesCSTDay 记账按 CST 取日键：昨天写的记录必须判为「今天
// 没签」，且日键直接由 cst.Day 决定——不是宿主时区的 Format。
func TestCheckinDoneTodayUsesCSTDay(t *testing.T) {
	p := New("")
	p.Add(&auth.Auth{UID: "u1"})

	now := time.Now()
	p.mu.Lock()
	p.byUID["u1"].lastCheckinDay = cst.Day(now.AddDate(0, 0, -1))
	p.mu.Unlock()
	if p.CheckinDoneToday("u1") {
		t.Error("昨天的记录不该算今天已签")
	}

	p.mu.Lock()
	p.byUID["u1"].lastCheckinDay = cst.Day(now)
	p.mu.Unlock()
	if !p.CheckinDoneToday("u1") {
		t.Error("今天的记录应算已签（日键必须与 cst.Day 同口径）")
	}
}

// TestCheckinDayKeyIsCST 日键必须由 CST 决定，与宿主时区无关。
//
// 关键样本：UTC 2026-11-13T17:30 = CST 2026-11-14T01:30。宿主跑 UTC 时按
// time.Now().Format 取日键会得到 11-13，而上游自然日已经是 11-14——补签会在
// CST 00:00~08:00 之间整天把「昨天签过」当「今天已签」，当天积分静默丢失。
func TestCheckinDayKeyIsCST(t *testing.T) {
	utcEvening := time.Date(2026, 11, 13, 17, 30, 0, 0, time.UTC)
	if got := checkinDayKey(utcEvening); got != "2026-11-14" {
		t.Errorf("checkinDayKey=%q want 2026-11-14（CST 已过零点）", got)
	}
	// 同一时刻换成 CST 表示，结论必须一致。
	if got := checkinDayKey(utcEvening.In(cst.Loc)); got != "2026-11-14" {
		t.Errorf("CST 表示下 checkinDayKey=%q want 2026-11-14", got)
	}
}

// TestCheckinDayPersists 记账要落盘：延后补签跨进程重启仍能跳过当天已签账号，
// 否则重启后会把当天的签到写调用再打一遍。
func TestCheckinDayPersists(t *testing.T) {
	dir := t.TempDir()
	fp := filepath.Join(dir, "state.json")
	p := New(fp)
	p.Add(&auth.Auth{UID: "u1"})
	p.NoteCheckinDone("u1")
	p.Flush()

	raw, err := os.ReadFile(fp)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(raw), `"last_checkin_day"`) {
		t.Fatalf("state.json 缺 last_checkin_day:\n%s", raw)
	}

	p2 := New(fp)
	p2.Add(&auth.Auth{UID: "u1"})
	if !p2.CheckinDoneToday("u1") {
		t.Error("重启后当天已签记账丢失（补签会重复打上游写接口）")
	}
}

// TestCheckinDayNotPersistedWhenEmpty 从未签过的账号不写出该键（空串与「今天没签」
// 同义，写出反而让人以为有记录）。
func TestCheckinDayNotPersistedWhenEmpty(t *testing.T) {
	dir := t.TempDir()
	fp := filepath.Join(dir, "state.json")
	p := New(fp)
	p.Add(&auth.Auth{UID: "u1"})
	p.NoteSuccess("u1") // 制造一次变更让 Flush 真的写盘（Flush 无变更不写）
	p.Flush()

	raw, err := os.ReadFile(fp)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(raw), `"last_checkin_day"`) {
		t.Errorf("未签到的账号不该写出 last_checkin_day:\n%s", raw)
	}
}
