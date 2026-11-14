// travel.go 猫猫旅行巡检状态机：随旅行时点（travel_hours，默认 09 点）对池内每个可用账号单趟推进一次。
// 无猫 → 同意协议 + 领养；有猫 → 按 travel/status 分派 派出 / 领奖 / 跳过。
package scheduler

import (
	"context"
	"log"
	"strings"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/cst"
	"workbuddy2api/internal/logfmt"
	"workbuddy2api/internal/upstream"
)

const (
	// travelLocationID 派出地点固定 4（古镇客栈）：4 个地点收益/时长区间完全相同，无最优解。
	travelLocationID = 4

	// travelStateIdle 空闲可派出；travelStateTraveling 在途；travelStateArrived 到站可领奖。
	travelStateIdle      = "idle"
	travelStateTraveling = "traveling"
	travelStateArrived   = "arrived"

	// buddyOpenPerRoundCap 单轮单账号最多开几个盲盒：开盒是消耗能量的写操作，
	// 上游 quota 异常（affordable 离谱）时不能变成无限消耗。正常单次上限是 5。
	buddyOpenPerRoundCap = 5
)

// travelAccountDelay 账号间限速：全量账号约 40s，避免上游风控。测试可置 0。
var travelAccountDelay = 800 * time.Millisecond

// activityAccountDelay 活跃上报账号间限速：与旅行同口径，避免上游风控。测试可置 0。
var activityAccountDelay = 800 * time.Millisecond

// activityReportGap 同一账号内连续上报之间的间隔：5 连发模拟同一会话多轮对话，
// 秒发易触发风控，故 1.5s 一条。测试可置 0。
var activityReportGap = 1500 * time.Millisecond

// sleepCtx 可取消的等待：ctx 取消立即返回 false（优雅停机不必等 sleep 醒来），
// 等满返回 true。d<=0 立即放行（测试把延迟置 0 时不白等）。
// 替换 time.Sleep：账号间/账号内限速值不变，只换等待方式。
func sleepCtx(ctx context.Context, d time.Duration) bool {
	if d <= 0 {
		return ctx.Err() == nil
	}
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// travelDay 返回 t 所属的上游自然日（CST），格式 2006-01-02。
// 走 internal/cst 的唯一定义：宿主时区不是 UTC+8 时（NAS / 海外 VPS），
// 直接用宿主时区算日键会让「今天」整体漂 8 小时。
func travelDay(t time.Time) string {
	return cst.Day(t)
}

// RunTravelNow 立即对池内所有可用账号执行一趟旅行巡检（无 ctx 的外部入口：
// 测试与潜在的手动触发——cmd 侧从未接线，不存在 cmd/travel 入口，部署验证
// 场景由 RunActivityNow 覆盖）。内部走 runTravel，取背景 ctx（不可取消，
// 语义与引入前 time.Sleep 版一致）。
func (s *Scheduler) RunTravelNow() {
	s.runTravel(context.Background())
}

// runTravel 旅行巡检遍历，随 ctx 取消立即退出。
// 禁用账号跳过；401/查询失败只跳过该账号本轮（不强刷 token，交 22:00 keepalive）；
// 账号间限速 travelAccountDelay（sleepCtx：取消时立即放弃后续账号）。
func (s *Scheduler) runTravel(ctx context.Context) {
	first := true
	for _, st := range s.cfg.Pool.List() {
		if st.Disabled || s.cfg.Pool.RiskPaused(st.UID) {
			continue
		}
		a := s.cfg.Pool.AuthByUID(st.UID)
		if a == nil || a.RefreshTokenValue() == "" {
			continue
		}
		if a.IsGlobal() {
			continue // D4 门控：global 无猫猫旅行体系，不发起任何上游调用
		}
		if !first {
			if !sleepCtx(ctx, s.accountGap(ctx, travelAccountDelay)) {
				return // 优雅停机：不等限速睡满，剩余账号下轮再巡
			}
		}
		first = false
		s.travelOne(a)
	}
}

// travelOne 单账号单趟状态机：查有无猫 + 查状态 + 最多一个动作，不轮询不等待。
func (s *Scheduler) travelOne(a *auth.Auth) {
	buddy, err := s.cfg.Upstream.BuddyInfo(a)
	if err != nil {
		log.Printf("travel %s: buddy-info: %v", logfmt.Label(a.UID, a.Nickname), err)
		return
	}
	if buddy == nil {
		s.travelAdopt(a)
		return
	}
	ts, err := s.cfg.Upstream.TravelStatus(a)
	if err != nil {
		log.Printf("travel %s: status: %v", logfmt.Label(a.UID, a.Nickname), err)
		return
	}
	switch ts.State {
	case travelStateArrived:
		// 到站先领奖；领完本轮不再派出/开盒（奖励动作与旅行动作分开，避免状态竞态）。
		s.travelClaim(a, ts)
	case travelStateIdle:
		// 空闲：先开盲盒再派出。开盒消耗能量换 Buddy，与旅行名额互不影响；
		// quota/开盒失败都不阻断派出（盲盒是增益，不是派出的前置）。
		s.travelOpenBoxes(a)
		s.travelDepart(a, ts)
	case travelStateTraveling:
		log.Printf("travel %s: skip (traveling record=%d)", logfmt.Label(a.UID, a.Nickname), ts.RecordID)
	default:
		log.Printf("travel %s: skip (unknown state %q)", logfmt.Label(a.UID, a.Nickname), ts.State)
	}
}

// travelOpenBoxes 空闲时开盲盒。只在 idle 调用：traveling 途中开盒会打乱状态机，
// arrived 需先领奖（本轮不 depart 也不开盒）；未知状态同样不开。
// quota 查询失败 / 能量不足 / 开盒失败都只记日志，由调用方继续派出。
func (s *Scheduler) travelOpenBoxes(a *auth.Auth) {
	label := logfmt.Label(a.UID, a.Nickname)
	q, err := s.cfg.Upstream.BuddyQuota(a)
	if err != nil {
		log.Printf("travel %s: buddy-quota: %v", label, err)
		return
	}
	n := q.Affordable
	if q.MaxOpenCount > 0 && q.MaxOpenCount < n {
		n = q.MaxOpenCount
	}
	if n > buddyOpenPerRoundCap {
		n = buddyOpenPerRoundCap
	}
	if n <= 0 {
		log.Printf("travel %s: skip buddy-open (energy insufficient affordable=%d)", label, q.Affordable)
		return
	}
	names, err := s.cfg.Upstream.BuddyOpen(a, n)
	if err != nil {
		log.Printf("travel %s: buddy-open count=%d: %v", label, n, err)
		return
	}
	if len(names) > 0 {
		log.Printf("travel %s: buddy-open ok count=%d names=%s", label, n, strings.Join(names, ","))
		return
	}
	log.Printf("travel %s: buddy-open ok count=%d", label, n)
}

// travelDepart 空闲且未达当日上限时派出（每日 1 次，自然日 00:00 CST 重置）。
func (s *Scheduler) travelDepart(a *auth.Auth, ts *upstream.TravelState) {
	if ts.DailyLimitReached {
		log.Printf("travel %s: skip (daily limit reached)", logfmt.Label(a.UID, a.Nickname))
		return
	}
	if err := s.cfg.Upstream.TravelDepart(a, travelLocationID); err != nil {
		log.Printf("travel %s: depart: %v", logfmt.Label(a.UID, a.Nickname), err)
		return
	}
	log.Printf("travel %s: depart ok location=%d", logfmt.Label(a.UID, a.Nickname), travelLocationID)
}

// travelClaim 到站领奖（必须带 record_id）。
func (s *Scheduler) travelClaim(a *auth.Auth, ts *upstream.TravelState) {
	if ts.RecordID == 0 {
		log.Printf("travel %s: claim skipped (arrived but no record_id)", logfmt.Label(a.UID, a.Nickname))
		return
	}
	reward, err := s.cfg.Upstream.TravelClaim(a, ts.RecordID)
	if err != nil {
		log.Printf("travel %s: claim record=%d: %v", logfmt.Label(a.UID, a.Nickname), ts.RecordID, err)
		return
	}
	log.Printf("travel %s: claim ok record=%d reward=%d", logfmt.Label(a.UID, a.Nickname), ts.RecordID, reward)
}

// travelAdopt 旅行巡检时领养：受 adoptTriedToday 当日防抖约束。
func (s *Scheduler) travelAdopt(a *auth.Auth) {
	s.adoptBuddy(a, false)
}

// travelAdoptForce 活跃上报补满对话量后领养：豁免 adoptTriedToday 当日防抖。
// 背景：旅行排程 09 点已领养且因对话量未达 skip，10 点活跃上报 5 连发把
// 对话量补满——此时是「门槛刚达成」的新状态，不算对上游重试轰炸，放行重试。
// 有猫账号 BuddyInfo 非空时直接跳过（不重复领养）。
func (s *Scheduler) travelAdoptForce(a *auth.Auth) {
	if a.IsGlobal() {
		return // D4 门控：国际版任务中心没有 first_buddy / chat_5，不发起领养调用
	}
	buddy, err := s.cfg.Upstream.BuddyInfo(a)
	if err != nil {
		log.Printf("activity %s: buddy-info: %v", logfmt.Label(a.UID, a.Nickname), err)
		return
	}
	if buddy != nil {
		return // 已有猫，无需领养
	}
	s.adoptBuddy(a, true) // force=true 豁免当日防抖
}

// adoptBuddy 无猫时领养：先同意协议（幂等）再 buddy/first。
// conversation 门槛未达标（HTTP 400 first_buddy task not completed yet）属预期行为，
// 记一次当日已试后静默跳过，不再重试。force=true 时豁免当日防抖（活跃上报补满对话量后重试）。
func (s *Scheduler) adoptBuddy(a *auth.Auth, force bool) {
	if !force && s.adoptTriedToday(a.UID) {
		return
	}
	if err := s.cfg.Upstream.BuddyAgreement(a); err != nil {
		log.Printf("travel %s: agreement: %v", logfmt.Label(a.UID, a.Nickname), err)
		return
	}
	err := s.cfg.Upstream.BuddyFirst(a)
	switch {
	case err == nil:
		log.Printf("travel %s: adopt ok (+300 credits)", logfmt.Label(a.UID, a.Nickname))
	case upstream.IsBuddyTaskIncomplete(err):
		s.markAdoptTried(a.UID)
		log.Printf("travel %s: adopt skipped (conversation threshold not reached, retry tomorrow)", logfmt.Label(a.UID, a.Nickname))
	default:
		log.Printf("travel %s: adopt: %v", logfmt.Label(a.UID, a.Nickname), err)
	}
}

// adoptTriedToday 该账号当日是否已判定领养门槛未达。
func (s *Scheduler) adoptTriedToday(uid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.adoptTried[uid] == travelDay(time.Now())
}

// markAdoptTried 记录该账号当日已尝试领养且未过门槛。
func (s *Scheduler) markAdoptTried(uid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.adoptTried[uid] = travelDay(time.Now())
}
