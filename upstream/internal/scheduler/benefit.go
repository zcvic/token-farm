// benefit.go 福利活动（新手礼包）的执行侧。
//
// 历史：这里原本还承载「官方补偿」（上游 PR #196 / issue #189）——排程是
// `if credit, err := ClaimCompensation(...); err == nil { log }`，让「没有补偿 / 已领过 /
// 真失败」三种情形同构，实测每天多一行**假的**「🎁 补偿领取 +0c」。补偿带截止日，
// 版本一度补上了「先查状态再决定是否领」的两端（排程 + 面板）。该活动已随 2026-10
// 结束，整条链路（状态查询 / 领取 / 排程 / 面板卡片）一并下线；面板侧仍保留手动兜底
// （server/services/tencent.py 的 claim_compensation），本包不再自动调用。
//
// 保留下面的新手礼包：它踩的是同一个坑、用同一份纪律：
// **要么成功、要么显形**——无待领静默（正确形态），查询/领取失败一律 WARN。
package scheduler

import (
	"log"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/logfmt"
	"workbuddy2api/internal/upstream"
)

// claimGiftQuiet 排程侧单账号新手礼包：先查状态，待领才领。
//
// 旧写法 `if credit, err := ClaimGift(a); err == nil { log }` 对「已领过」也返回
// (0, nil)（上游 200 + 无 credit 字段），于是每个已领号每天都打一行假的
// 「🎁 新手礼包 +0c」。线上 2026-09-16~10-16 实测：日志有、余额流水一次 delta 都没有。
//
// 待领才领：无活动 / 已领 / 无额度静默；查询与领取失败 WARN（含截止日，礼包也带有效期）。
// 返回本次到账 credit（0 = 未领或没领到）。
func (s *Scheduler) claimGiftQuiet(a *auth.Auth) int64 {
	if a == nil {
		return 0
	}
	label := logfmt.Label(a.UID, a.Nickname)
	src, err := s.cfg.Upstream.GetGiftStatus(a)
	if err != nil {
		// 已知没有个人礼包体系的账号（企业版 / 已禁用）查询失败属预期，静默——
		// 否则每天固定刷出几行噪声；其余号的查询失败必须 WARN（旧盲领正是无痕）。
		// 国际号不在此列：它的礼包查询在 workbuddy.ai 上是正常 200（billing 域按 realm 走 base）。
		if upstream.IsEnterprise(a) || s.accountDisabled(a.UID) {
			return 0
		}
		log.Printf("WARN: streak-bonus %s: 礼包状态查询失败: %v", label, err)
		return 0
	}
	if !src.Pending() {
		return 0
	}
	// 本期（同 uid + 同 end_time）已试过就不再重复 claim：上游的 status 端点对
	// 「已领过」有时仍回 claimed:false（2026-10-20 实测），单靠 Pending() 会变成
	// 每次签到都重打一遍全池的 claim。
	guardKey := "gift|" + a.UID + "|" + src.EndTime
	if s.benefitGuarded(guardKey) {
		return 0
	}
	credit, err := s.cfg.Upstream.ClaimGift(a)
	s.markBenefitGuarded(guardKey) // 无论成败都记：重试交给下一期或面板手动
	if err != nil {
		log.Printf("WARN: streak-bonus %s: 礼包领取失败（可领 %dc，截止 %s）: %v",
			label, src.CreditNum, src.EndTime, err)
		return 0
	}
	// ⭐ 上游对「已领过」回 200 + 无 credit 字段（与补偿同一个坑）：旧代码用
	// src.CreditNum 兜底，把「没发」写成「🎁 +1000c 已到账」。现在只认真实回执：
	// credit<=0 就是本次到账 0，静默返回。
	if credit <= 0 {
		return 0
	}
	log.Printf("streak-bonus %s: 🎁 新手礼包 +%dc（截止 %s）", label, credit, src.EndTime)
	return credit
}

// accountDisabled 账号是否已被池标记禁用（企业号之外的另一类「查不到礼包属正常」）。
// 池里查不到（账号已删除）按未禁用处理，让调用方走 WARN 分支。
func (s *Scheduler) accountDisabled(uid string) bool {
	if s.cfg.Pool == nil {
		return false
	}
	st, ok := s.cfg.Pool.Status(uid)
	return ok && st.Disabled
}

// benefitGuarded 报告本期福利是否已在本进程内尝试过。
func (s *Scheduler) benefitGuarded(key string) bool {
	s.benefitGuardMu.Lock()
	defer s.benefitGuardMu.Unlock()
	return s.benefitGuard[key]
}

// markBenefitGuarded 记下「本期已尝试」。懒初始化 map（零值 Scheduler 也可用）。
func (s *Scheduler) markBenefitGuarded(key string) {
	s.benefitGuardMu.Lock()
	defer s.benefitGuardMu.Unlock()
	if s.benefitGuard == nil {
		s.benefitGuard = make(map[string]bool)
	}
	s.benefitGuard[key] = true
}
