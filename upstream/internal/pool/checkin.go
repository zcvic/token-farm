// 签到记账：记录「今天已签到」这一事实，供延后补签跳过已签账号。
//
// 只记日期不记时刻：跨零点自然过期，且签到与冷却/禁用域正交（本文件不碰
// disabled/until/reason）。持久化在 stateAccount.LastCheckinDay：进程重启后
// 延后补签仍能跳过当天已签的账号，不会重复打上游写接口。
package pool

import (
	"time"

	"workbuddy2api/internal/cst"
)

// checkinDayKey 签到记账的日键：按 CST 取自然日（上游日界）。
//
// 抽成函数是为了可测：直接写 cst.Day(time.Now()) 的话，宿主时区恰好是 CST 时
// 「按 CST」与「按宿主时区」两种实现输出完全相同，测试看不出区别（容器跑 UTC
// 时才会暴露——那时 CST 00:00~08:00 之间会把昨天当今天，补签整天被跳过）。
func checkinDayKey(now time.Time) string {
	return cst.Day(now)
}

// NoteCheckinDone 标记账号今日已签到（签到成功与上游「今天已签到」幂等拒绝都算）。
// 日期按 CST 取（见 checkinDayKey）：上游自然日，宿主时区换了也不能把昨天的记录
// 当今天。
func (p *Pool) NoteCheckinDone(uid string) {
	p.mu.Lock()
	defer p.mu.Unlock()
	e, ok := p.byUID[uid]
	if !ok {
		return
	}
	if day := checkinDayKey(time.Now()); e.lastCheckinDay != day {
		e.lastCheckinDay = day
		p.dirty.Store(true)
	}
}

// CheckinDoneToday 报告该账号今天是否已签到（本地记账；成功与上游幂等拒绝都算）。
// 供延后补签在重试前跳过已经签成的账号——补签是写操作，能少一次就少一次。
func (p *Pool) CheckinDoneToday(uid string) bool {
	p.mu.RLock()
	defer p.mu.RUnlock()
	e, ok := p.byUID[uid]
	if !ok {
		return false
	}
	return e.lastCheckinDay == checkinDayKey(time.Now())
}
