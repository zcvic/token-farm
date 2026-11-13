// 签到延后补签：主批结束后把「上游瞬时拒绝」的账号排进内存队列，在 10 / 40 分钟
// 后各补签一次。
//
// 为什么需要独立于 checkinWithRetry 的内联退避：后者只等 5s/15s/60s，而线上实测
// 的上游形态是 `http 500 {"code":10001,"msg":"签到失败，请稍后重试"}`——这类要求
// 「稍后」的失败需要分钟级沉降，一分半内三次全败，之后就要等下一个签到整点
// （最长 12 小时），中间这段时间账号当天的签到就是没有的（漏一天就是断连登）。
//
// 为什么只有两轮：下一个签到整点是天然的第二次机会，再加轮次收益递减，而每轮都是
// 真实写调用。10 分钟覆盖「刚结算完的抖动」，40 分钟覆盖「上游处理慢」；两轮之后
// 仍失败基本是账号或上游的系统性问题，靠重试解决不了，落 WARN 让运维看见。
//
// 变量而非常量：测试要把它压到毫秒级，避免真等 40 分钟。
package scheduler

import (
	"context"
	"log"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/logfmt"
	"workbuddy2api/internal/upstream"
)

var checkinLateRetryDelays = []time.Duration{10 * time.Minute, 40 * time.Minute}

// lateRetryWait 返回第 round 轮补签相对上一轮实际要睡的时长。
//
// checkinLateRetryDelays 是**从本批结束起算的绝对偏移**（10m、40m），所以每轮要睡
// 的是与上一轮的差值。写成函数是为了能被单测钉住：直接内联 `[round]-prev` 时，
// 漏掉 `-prev` 只是「第二轮从第一轮结束时起算」，行为差异藏在分钟级时间里，
// 功能测试看不出来（实际变成 10m 后 40m 再补，等于 50m 才补第二次）。
func lateRetryWait(round int, prev time.Duration) time.Duration {
	return checkinLateRetryDelays[round] - prev
}

// checkinOne 对单个 CN 账号执行一次签到（不含余额查询、不含 token 预刷新）。
//
// 「今天已签到」是幂等成功（上游对重复签到返回 code!=0），归 CheckinAlready 且
// err 为 nil；真失败才带 err，供调用方判断可否重试。两种「今天确实已签」的形态
// （签到成功 / 幂等拒绝）都落 NoteCheckinDone——延后补签据此跳过已签账号。
//
// 抽出来是为了让「排程主批」与「延后补签」共用同一份判据——两处各写一遍必然漂移，
// 而漂移的表现就是补签把幂等拒绝当成失败（白打写调用），或者把真失败当成已签
// （当天积分静默丢失）。
func (s *Scheduler) checkinOne(a *auth.Auth, uid string) (CheckinStatus, error) {
	label := logfmt.Label(uid, a.Nickname)
	if err := s.cfg.Upstream.DailyCheckin(a); err != nil {
		if upstream.IsAlreadyCheckin(err) {
			s.cfg.Pool.NoteCheckinDone(uid)
			log.Printf("checkin %s: 今天已签到（幂等）", label)
			return CheckinAlready, nil
		}
		return CheckinFail, err
	}
	// 成功也落一行：此前静默，排查「签到到底跑没跑」时无迹可循（幂等行只在重复
	// 触发时出现，正常成功反而没有日志）。
	s.cfg.Pool.NoteCheckinDone(uid)
	log.Printf("checkin %s: 签到成功", label)
	return CheckinOK, nil
}

// checkinWithRetry 执行签到，遇到上游瞬时失败（HTTP 5xx / code 10001）按
// checkinRetryDelays 原地退避重试；ctx 取消立即返回最后一次结论。
//
// 秒级退避只覆盖「一闪而过」的抖动；分钟级的「请稍后重试」交给 startCheckinLateRetry。
func (s *Scheduler) checkinWithRetry(ctx context.Context, a *auth.Auth, uid string) (CheckinStatus, error) {
	st, err := s.checkinOne(a, uid)
	label := logfmt.Label(uid, a.Nickname)
	for i, delay := range checkinRetryDelays {
		if st != CheckinFail || !checkinRetryable(err) {
			return st, err
		}
		log.Printf("checkin %s: %v — %s 后重试(%d/%d)", label, err, delay, i+1, len(checkinRetryDelays))
		if !sleepCtx(ctx, delay) {
			return st, err
		}
		st, err = s.checkinOne(a, uid)
	}
	return st, err
}

// startCheckinLateRetry 把本轮瞬时失败的账号排进后台补签队列。
//
// 不阻塞调用方：runBatch 要等各任务返回才继续下一批，阻塞会让下一个整点的整批
// 任务被跳过。ctx 取消即退，不拖住优雅停机。
func (s *Scheduler) startCheckinLateRetry(ctx context.Context, uids []string) {
	if len(uids) == 0 || len(checkinLateRetryDelays) == 0 {
		return
	}
	pending := append([]string(nil), uids...)
	log.Printf("[checkin] %d 个账号本轮瞬时失败，将在 %s、%s 后各补签一次（期间整点排程照常）",
		len(pending), checkinLateRetryDelays[0], checkinLateRetryDelays[len(checkinLateRetryDelays)-1])
	go s.checkinLateRetryLoop(ctx, pending)
}

func (s *Scheduler) checkinLateRetryLoop(ctx context.Context, pending []string) {
	prev := time.Duration(0)
	for round := 0; round < len(checkinLateRetryDelays); round++ {
		// 延迟是**绝对偏移**（10m、40m），所以这里睡的是与上一轮的差值。
		if !sleepCtx(ctx, lateRetryWait(round, prev)) {
			return // ctx 取消：停机，不补
		}
		prev = checkinLateRetryDelays[round]

		var still []string
		for _, uid := range pending {
			if ctx.Err() != nil {
				return
			}
			// 已签（含用户自己签了、或上一轮补签成功）就不再打上游。
			if s.cfg.Pool.CheckinDoneToday(uid) {
				continue
			}
			a := s.cfg.Pool.AuthByUID(uid)
			if a == nil || a.RefreshTokenValue() == "" || a.IsGlobal() {
				continue
			}
			// 与主批同一条跳过规则：禁用/风控暂停的账号不签（补签也不该绕过）。
			if st, ok := s.cfg.Pool.Status(uid); ok && (st.Disabled || s.cfg.Pool.RiskPaused(uid)) {
				continue
			}
			st, err := s.checkinOne(a, uid)
			switch {
			case st != CheckinFail:
				log.Printf("[checkin] 补签成功 %s（第 %d 轮 / 共 %d 轮）",
					logfmt.Label(uid, a.Nickname), round+1, len(checkinLateRetryDelays))
			case checkinRetryable(err):
				still = append(still, uid)
			default:
				// 非瞬时错误（业务拒绝、session dead 等）：重试不会有不同结果，
				// 本轮就放弃它，免得每天多打几轮注定失败的写调用。
				log.Printf("[checkin] %s 补签遇到不可重试的错误，放弃补签（%v）",
					logfmt.Label(uid, a.Nickname), err)
			}
		}
		pending = still
		if len(pending) == 0 {
			return
		}
	}
	log.Printf("WARN: [checkin] 补签 %d 轮后仍有 %d 个账号失败（等下一个签到整点再试）：%s",
		len(checkinLateRetryDelays), len(pending), joinUIDs(pending))
}

// joinUIDs 只打 uid 前 8 位：日志里出现完整 uuid 没有额外信息，短前缀与面板一致。
func joinUIDs(uids []string) string {
	out := ""
	for i, u := range uids {
		if i > 0 {
			out += ", "
		}
		if len(u) > 8 {
			u = u[:8]
		}
		out += u
	}
	return out
}
