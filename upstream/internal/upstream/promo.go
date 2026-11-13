// promo.go 解析 /v3/config 的 data.modelPromotions（限时优惠），把「当前生效」的
// 折扣挂到模型目录条目上，供 /v1/models 透出 promo_factor / promo_credits /
// promo_label（issue #203）。
//
// 口径分工：`credits` 是上游**牌价**倍率（转正后基准），`promo_*` 才是此刻生效的
// 折扣。WorkBuddy 客户端显示的是生效价，第三方启动器只读 /v1/models——不把这组
// 字段透出去，限时免费的模型会被显示成收费，或者显示旧牌价。
package upstream

import (
	"encoding/json"
	"strconv"
	"strings"
	"time"

	"workbuddy2api/internal/cst"
)

// v3PromoBadge 优惠徽章（「限时免费」/「夜间折扣」/「错峰使用」）。
type v3PromoBadge struct {
	Label string `json:"label"`
}

// v3PromoDiscount 可计算的折扣：factor 是系数（0=免费、0.5=五折），
// discountedCredits 是折扣后倍率的展示原文（"0x" / "0.50x"）。
type v3PromoDiscount struct {
	DiscountedCredits string  `json:"discountedCredits"`
	Factor            float64 `json:"factor"`
}

// v3PromoWindow 每日时段窗口（"HH:MM" → "HH:MM"，可跨午夜）。
type v3PromoWindow struct {
	Start string `json:"start"`
	End   string `json:"end"`
}

// v3PromoSchedule 优惠生效期：validFrom/validUntil 是绝对时间（RFC3339，可缺省），
// daily 是每日时段（空=全天）。timezone 实测恒 Asia/Shanghai，只作记录。
type v3PromoSchedule struct {
	Daily      []v3PromoWindow `json:"daily"`
	Timezone   string          `json:"timezone"`
	ValidFrom  string          `json:"validFrom"`
	ValidUntil string          `json:"validUntil"`
}

// v3ModelPromotion /v3/config data.modelPromotions 单条优惠定义（参考仓 2026-09-23
// 实测 7 条：deepseek 系错峰五折、glm-5.2 夜间五折、hy3 与 hy4-preview-f 限时免费）。
//
// discount 只在部分条目上存在：有 factor 的可算生效价；「错峰使用」类只有时段文案
// （factor 藏在 hover 文本里，无机器可读值），只透标签。modelPromotions 里还夹着
// **纯模型定义**（无 modelIds / discount，只有 id+name+tags）——本网关不移植媒体
// 旁路，这类条目的 ModelIDs 为空，天然不命中任何目录项，无需特判。
type v3ModelPromotion struct {
	Enabled  bool             `json:"enabled"`
	Priority int              `json:"priority"`
	ModelIDs []string         `json:"modelIds"`
	Badge    *v3PromoBadge    `json:"badge"`
	Discount *v3PromoDiscount `json:"discount"`
	Schedule *v3PromoSchedule `json:"schedule"`
}

// promoClock 解析 "HH:MM" 为当日分钟数；坏值返回 (-1, false)。
func promoClock(hhmm string) (int, bool) {
	parts := strings.Split(hhmm, ":")
	if len(parts) != 2 {
		return -1, false
	}
	h, err1 := strconv.Atoi(strings.TrimSpace(parts[0]))
	m, err2 := strconv.Atoi(strings.TrimSpace(parts[1]))
	if err1 != nil || err2 != nil || h < 0 || h > 24 || m < 0 || m > 59 {
		return -1, false
	}
	return h*60 + m, true
}

// promoActive 评估优惠在 now 是否生效：enabled + validFrom/validUntil 内 + 落在
// 任一 daily 窗口（支持跨午夜，如 23:00→7:50）。schedule 为 nil 视为全天生效。
//
// 时段按 CST 判定（上游恒 Asia/Shanghai）：用 cst.Loc 而不是宿主时区，容器跑在
// UTC 时否则会把夜间折扣算到白天。
func promoActive(p *v3ModelPromotion, now time.Time) bool {
	if !p.Enabled {
		return false
	}
	sc := p.Schedule
	if sc == nil {
		return true
	}
	// 时间串解析失败时按「没有这条约束」处理：宁可能多展示一个折扣标签，也不要
	// 因为一个坏字段把整条优惠吞掉（展示层，不是计费层）。
	if sc.ValidFrom != "" {
		if from, err := time.Parse(time.RFC3339, sc.ValidFrom); err == nil && now.Before(from) {
			return false
		}
	}
	if sc.ValidUntil != "" {
		if until, err := time.Parse(time.RFC3339, sc.ValidUntil); err == nil && !now.Before(until) {
			return false
		}
	}
	if len(sc.Daily) == 0 {
		return true
	}
	local := now.In(cst.Loc)
	cur := local.Hour()*60 + local.Minute()
	for _, w := range sc.Daily {
		st, ok1 := promoClock(w.Start)
		ed, ok2 := promoClock(w.End)
		if !ok1 || !ok2 {
			continue
		}
		if st <= ed {
			if cur >= st && cur < ed {
				return true
			}
		} else if cur >= st || cur < ed { // 跨午夜（23:00→7:50）
			return true
		}
	}
	return false
}

// parseV3Promotions 从 /v3/config 原文里取 data.modelPromotions。解析失败或窄表
// 形态（data 是数组）→ nil：优惠是**展示增强**，读不到就按「没有优惠」处理，
// 不能让整个模型目录跟着失败。
func parseV3Promotions(raw []byte) []v3ModelPromotion {
	var env struct {
		Data struct {
			ModelPromotions []v3ModelPromotion `json:"modelPromotions"`
		} `json:"data"`
	}
	if err := json.Unmarshal(raw, &env); err != nil {
		return nil
	}
	return env.Data.ModelPromotions
}

// applyModelPromotions 把当前生效的优惠挂到目录条目：同模型多条命中取 priority
// 最高（实测 glm-5.2 白天 badge-only 与夜间五折靠 priority + daily 双轨切换）。
// 无 discount 对象的条目也挂标签（错峰类），PromoFactor 留 nil。
//
// 目录外模型（如同名 global 变体）不挂：优惠表是平台级的，条目不在本次目录里
// 就没有可展示的位置。
func applyModelPromotions(infos []ModelInfo, promos []v3ModelPromotion, now time.Time) {
	if len(promos) == 0 || len(infos) == 0 {
		return
	}
	idx := make(map[string]int, len(infos))
	for i := range infos {
		idx[infos[i].ID] = i
	}
	best := make(map[string]int, len(infos)) // id → promos 下标
	for i := range promos {
		p := &promos[i]
		if !promoActive(p, now) {
			continue
		}
		for _, id := range p.ModelIDs {
			if _, ok := idx[id]; !ok {
				continue
			}
			if j, seen := best[id]; seen && promos[j].Priority >= p.Priority {
				continue
			}
			best[id] = i
		}
	}
	for id, j := range best {
		p := &promos[j]
		mi := &infos[idx[id]]
		if p.Badge != nil {
			mi.PromoLabel = p.Badge.Label
		}
		if p.Discount != nil {
			f := p.Discount.Factor
			mi.PromoFactor = &f
			mi.PromoCredits = p.Discount.DiscountedCredits
		}
	}
}

// applyPromotionsFromRaw 便捷封装：解析 raw 里的优惠表并挂到 infos 上。
// v3 目录（CN FetchModels 与 global 探测）共用同一条 /v3/config 响应形态。
func applyPromotionsFromRaw(infos []ModelInfo, raw []byte) {
	applyModelPromotions(infos, parseV3Promotions(raw), time.Now())
}
