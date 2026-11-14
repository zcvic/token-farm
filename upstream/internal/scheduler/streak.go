// streak.go 连登管家：签到排程后自动补签 → 领礼包 → 兑换所有已解锁档位 → 抽完次数。
//
// 背景（2026-09-12）：成长中心连登档位（7d/14d/28d）按连续登录天数解锁，兑换发
// credit/energy/补签卡/抽奖次数；抽奖次数只能从兑换获得。兑换按钮在 UI 上恒可点，
// 但未解锁时服务端 403「连续登录天数不足」——所以放在每日签到后跑一遍（幂等），
// 到天数那天自动完成「兑换 → 抽奖」闭环，无需人工盯。
//
// 与旧链路（claimGrowthRewards 挂在活跃上报之后）的差别：
//   - **触发点解耦**（A-5）：以前 activity 关闭、上报失败或不足 ActivityReportCount
//     条时完全不领；现在挂在签到排程末尾，activity 只负责点亮与自检。
//   - **全档兑换**（A-3）：以前每日只挑最高一档；现在遍历 tiers 跳过 locked/claimed，
//     其余全部 redeem（跨档连领是官方正常态）。
//   - **抽完次数**（A-4）：以前只抽一次；现在按 chances 循环抽完。
//   - **本月漏签全补**（A-2）：以前只判昨日；现在从本月最近的漏签往回补。
package scheduler

import (
	"log"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/logfmt"
	"workbuddy2api/internal/upstream"
)

// lotteryDrawPerRoundCap 单轮单账号最多抽几次：抽奖是消耗性写操作，chances 异常
// （上游脏数据 / 活动赠送叠加）时不能变成死循环。正常一次兑换最多送 1~2 次。
const lotteryDrawPerRoundCap = 50

// RunStreakBonusNow 对所有可用账号执行「补签 + 礼包 + 兑换 + 抽奖」（幂等：
// locked/无次数/已领自动跳过）。由签到排程末尾调用；面板也可手动触发。
//
// 门控：
//   - global：连登/补签/兑换/抽奖走 CN web base，实测 global GET streak 500，
//     故只做**礼包**（礼包是 billing 域、按 realm 走 base，国际号实测 200）。
//   - 企业版：无个人成长体系（上游对 growth 端点直接拒绝），整号跳过。
//   - 无 access token：没有可用的上游凭证，跳过。
func (s *Scheduler) RunStreakBonusNow() {
	for _, st := range s.cfg.Pool.List() {
		if st.Disabled {
			continue
		}
		a := s.cfg.Pool.AuthByUID(st.UID)
		if a == nil || a.AccessTokenValue() == "" {
			continue
		}
		if a.IsGlobal() {
			// D4 门控只覆盖 CN 成长体系（连登/补签/兑换/抽奖走 CN web base）。礼包是
			// billing 域、按 realm 走 base，国际号实测 200——一并跳过就会静默漏领国际区活动。
			s.claimGiftQuiet(a)
			continue
		}
		if upstream.IsEnterprise(a) {
			continue // 企业版门控：无个人成长体系
		}
		s.streakBonusAccount(a)
	}
}

// streakBonusAccount 单账号：补签保连登 → 礼包 → 兑换所有已解锁档位 → 抽完所有 chances。
// 返回 true 表示本轮真的发过写请求（补签 / 兑换 / 抽奖），供手动入口的「当日已处理」闸使用。
func (s *Scheduler) streakBonusAccount(a *auth.Auth) bool {
	// 0. 补签保连登：本月漏签且有补签卡则从最近一天往回补（连续天数一断就要重攒 7 天）。
	// 返回的 state 只在「没补成功」时有效（补签改了天数，必须重读）；nil 表示需要重新读。
	full, wrote := s.makeupMissed(a)
	// 0.5 礼包：走「先查状态 → 待领才领」（见 benefit.go）。
	// 盲领会把「无活动/已领过」打成假的「+0c」成功行，真失败反倒无痕。
	s.claimGiftQuiet(a)

	if full == nil {
		var err error
		full, err = s.cfg.Upstream.GrowthStreakFull(a)
		if err != nil {
			log.Printf("WARN: streak-bonus %s: %v", logfmt.Label(a.UID, a.Nickname), err)
			return wrote
		}
	}
	label := logfmt.Label(a.UID, a.Nickname)
	statuses := map[string]string{
		"7d":  full.RedemptionStatus.Tier7dStatus,
		"14d": full.RedemptionStatus.Tier14dStatus,
		"28d": full.RedemptionStatus.Tier28dStatus,
	}
	for _, tier := range full.RedemptionStatus.Tiers {
		// 状态以 tiers 里逐档的字段为准；缺失时回落 tier_7d/14d/28d_status。
		// locked = 天数未达（写必然 403），claimed = 本月已领（写必然 409）——都不写。
		status := statuses[tier.Tier]
		if status == "locked" || status == "claimed" {
			continue
		}
		wrote = true // 发了写请求（无论成败），当日不再重复
		res, err := s.cfg.Upstream.GrowthRedeem(a, tier.Tier, "")
		switch {
		case err == nil:
			log.Printf("streak-bonus %s: ★ 兑换 %s 档（+%dc +%de 卡×%d 抽奖×%d）",
				label, tier.Tier, res.CreditGranted, res.EnergyGranted, res.CardsGranted, res.ChancesGranted)
		case upstream.IsRedeemAlreadyClaimed(err) || upstream.IsRedeemNotEnoughDays(err):
			// 服务端正常态：状态字段滞后（上游 status 与 redeem 判定偶有窗口差）。
			// 静默——每档每天一行 skip 会把日志刷满。
		default:
			log.Printf("WARN: streak-bonus %s: redeem %s: %v", label, tier.Tier, err)
		}
	}
	// 抽奖：按当前 chances 全抽完（兑换刚发的次数已在服务端累加）。
	chances, err := s.cfg.Upstream.GrowthLotteryChances(a)
	if err != nil {
		log.Printf("WARN: streak-bonus %s: lottery-chances: %v", label, err)
		return wrote
	}
	if chances <= 0 {
		return wrote
	}
	if chances > lotteryDrawPerRoundCap {
		log.Printf("WARN: streak-bonus %s: 抽奖次数 %d 超过单轮上限 %d，本轮只抽 %d 次",
			label, chances, lotteryDrawPerRoundCap, lotteryDrawPerRoundCap)
		chances = lotteryDrawPerRoundCap
	}
	for i := 0; i < chances; i++ {
		wrote = true
		res, err := s.cfg.Upstream.GrowthLotteryDraw(a, "") // 每次自动新 client_token
		if err != nil {
			if upstream.IsLotteryNoChance(err) || upstream.IsLotteryDisabled(err) {
				return wrote // 正常态：次数已用完 / 活动关闭，不再续抽
			}
			log.Printf("WARN: streak-bonus %s: lottery draw: %v", label, err)
			return wrote
		}
		log.Printf("streak-bonus %s: 🎲 第%d抽 %s（%s）", label, i+1, res.PrizeName, res.PrizeType)
	}
	log.Printf("streak-bonus %s: 抽奖完成 %d 次", label, chances)
	return wrote
}

// makeupMissed 本月漏签且有补签卡时自动补签（保住连登连续天数）。
// 从最近一天往回补（最近的最值钱），最多补到卡用光或本轮上限。
// 上游拒绝即停：注册前的日子同样是 0 分，无法与漏签区分，只能由拒绝兜底。
// 无卡 / 无漏签 / 查询失败均静默（不影响主流程）。
//
// 返回：
//   - state：补签前的连登状态（已读过一次，调用方可直接复用省一次往返）；nil 表示
//     没读过（热力图失败/无漏签），调用方需自己再读。
//   - wrote：本轮至少成功补签一天（补签改了天数，state 已过期，调用方必须重读）。
func (s *Scheduler) makeupMissed(a *auth.Auth) (*upstream.GrowthStreakFull, bool) {
	broken, err := s.cfg.Upstream.HeatmapBrokenDates(a)
	if err != nil || len(broken) == 0 {
		return nil, false
	}
	full, err := s.cfg.Upstream.GrowthStreakFull(a)
	if err != nil {
		return nil, false
	}
	if full.MakeupCards.Balance <= 0 {
		// 有漏签但无卡：状态本身有效，直接交给调用方挑档（省一次重复读）。
		return full, false
	}
	label := logfmt.Label(a.UID, a.Nickname)
	n := full.MakeupCards.Balance
	if n > len(broken) {
		n = len(broken)
	}
	done := false
	for i := 0; i < n; i++ {
		// 每轮上限（schedule.makeup_cards_per_round，0 = 不限）：补签是改账号历史的写操作，
		// 保守部署可以一轮只补一张，剩下的交给次日签到再补。
		if limit := s.MakeupCardsPerRound(); limit > 0 && i >= limit {
			log.Printf("streak-bonus %s: 本轮已用 %d 张补签卡（schedule.makeup_cards_per_round 上限），剩余漏签下一轮继续",
				label, i)
			break
		}
		d := broken[len(broken)-1-i]
		if err := s.cfg.Upstream.UseMakeupCard(a, d); err != nil {
			// 注册前 / 已补过 / 无卡：上游拒绝即停，不猜、不跳过继续（继续只会连环失败）。
			log.Printf("WARN: streak-bonus %s: 补签 %s 失败: %v", label, d, err)
			break
		}
		done = true
		log.Printf("streak-bonus %s: ★ 已用补签卡补签 %s（保连登）", label, d)
	}
	if done {
		// 天数已被改写，旧 state 不可用——让调用方重读。
		return nil, true
	}
	return full, false
}
