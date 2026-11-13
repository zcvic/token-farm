package upstream

import (
	"encoding/json"
	"testing"
	"time"

	"workbuddy2api/internal/cst"
)

// promoAt 构造一个 CST 时刻（promo 时段判定按 CST，与宿主时区无关）。
func promoAt(hour, min int) time.Time {
	return time.Date(2026, 11, 13, hour, min, 0, 0, cst.Loc)
}

// promoWindow 便捷构造单条 daily 窗口。
func promoWindow(start, end string) *v3PromoSchedule {
	return &v3PromoSchedule{Daily: []v3PromoWindow{{Start: start, End: end}}, Timezone: "Asia/Shanghai"}
}

// TestPromoActiveSchedule promo 生效判定：enabled / validFrom / validUntil /
// daily 窗口（含跨午夜）。任何一条不满足都算「此刻不生效」。
func TestPromoActiveSchedule(t *testing.T) {
	night := &v3ModelPromotion{Enabled: true, Schedule: promoWindow("23:00", "7:50")}
	cases := []struct {
		name string
		p    *v3ModelPromotion
		now  time.Time
		want bool
	}{
		{"禁用一律不生效", &v3ModelPromotion{Enabled: false}, promoAt(12, 0), false},
		{"无 schedule 视为全天生效", &v3ModelPromotion{Enabled: true}, promoAt(12, 0), true},
		{"跨午夜窗口内（23:30）", night, promoAt(23, 30), true},
		{"跨午夜窗口内（次日 7:00）", night, promoAt(7, 0), true},
		{"跨午夜窗口外（12:00）", night, promoAt(12, 0), false},
		{"窗口右端不含（7:50）", night, promoAt(7, 50), false},
		{"窗口左端含（23:00）", night, promoAt(23, 0), true},
		{"非跨午夜窗口内（12:00）", &v3ModelPromotion{Enabled: true, Schedule: promoWindow("9:00", "18:00")}, promoAt(12, 0), true},
		{"非跨午夜窗口右端不含（18:00）", &v3ModelPromotion{Enabled: true, Schedule: promoWindow("9:00", "18:00")}, promoAt(18, 0), false},
		{"非跨午夜窗口外（8:59）", &v3ModelPromotion{Enabled: true, Schedule: promoWindow("9:00", "18:00")}, promoAt(8, 59), false},
		{
			"validFrom 未到",
			&v3ModelPromotion{Enabled: true, Schedule: &v3PromoSchedule{ValidFrom: "2026-12-01T00:00:00+08:00"}},
			promoAt(12, 0), false,
		},
		{
			"validUntil 已过",
			&v3ModelPromotion{Enabled: true, Schedule: &v3PromoSchedule{ValidUntil: "2026-11-01T00:00:00+08:00"}},
			promoAt(12, 0), false,
		},
		{
			"坏时刻串的窗口跳过",
			&v3ModelPromotion{Enabled: true, Schedule: promoWindow("abc", "7:50")},
			promoAt(12, 0), false,
		},
		{
			"无 daily 但有 validUntil 未过 → 全天生效",
			&v3ModelPromotion{Enabled: true, Schedule: &v3PromoSchedule{ValidUntil: "2026-12-01T00:00:00+08:00"}},
			promoAt(12, 0), true,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := promoActive(c.p, c.now); got != c.want {
				t.Errorf("promoActive=%v want %v", got, c.want)
			}
		})
	}
}

// TestPromoActiveCSTNotHostZone 时段按 CST 判定：同一时刻换个时区表示，结论不变
// （容器跑 UTC 时不能把夜间折扣算到白天）。
func TestPromoActiveCSTNotHostZone(t *testing.T) {
	raw := `{"enabled":true,"schedule":{"daily":[{"start":"23:00","end":"7:50"}],"timezone":"Asia/Shanghai"}}`
	var p v3ModelPromotion
	if err := json.Unmarshal([]byte(raw), &p); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	// 23:30 CST = 15:30 UTC：用 UTC 表示传入，结论必须仍是「生效」。
	utc := time.Date(2026, 11, 13, 15, 30, 0, 0, time.UTC)
	if !promoActive(&p, utc) {
		t.Error("23:30 CST 应命中夜间窗口（判定依赖 CST，不依赖入参时区）")
	}
	// 12:00 CST = 04:00 UTC：窗口外。
	if promoActive(&p, time.Date(2026, 11, 13, 4, 0, 0, 0, time.UTC)) {
		t.Error("12:00 CST 不该命中夜间窗口")
	}
}

// promoTargets 造一份目录（只关心 id 与牌价）。
func promoTargets(ids ...string) []ModelInfo {
	out := make([]ModelInfo, 0, len(ids))
	for _, id := range ids {
		out = append(out, ModelInfo{ID: id, Credits: "x0.79"})
	}
	return out
}

// TestApplyModelPromotions 挂载口径：同模型多条命中取 priority 最高；factor=0
// （限时免费）必须落成非 nil；只有 badge 的条目只挂标签。
func TestApplyModelPromotions(t *testing.T) {
	raw := `[
		{"enabled":true,"priority":50,"modelIds":["glm-5.2"],"badge":{"label":"错峰使用"}},
		{"enabled":true,"priority":100,"modelIds":["glm-5.2"],"badge":{"label":"夜间折扣"},
		 "discount":{"discountedCredits":"0.50x","factor":0.5}},
		{"enabled":true,"priority":10,"modelIds":["hy3"],"badge":{"label":"限时免费"},
		 "discount":{"discountedCredits":"0x","factor":0}}
	]`
	var promos []v3ModelPromotion
	if err := json.Unmarshal([]byte(raw), &promos); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	infos := promoTargets("glm-5.2", "hy3", "paid-one")
	applyModelPromotions(infos, promos, promoAt(12, 0))

	// priority 100 那条胜出（两条都没有 schedule，视为全天生效）。
	if infos[0].PromoLabel != "夜间折扣" || infos[0].PromoCredits != "0.50x" {
		t.Errorf("glm-5.2 promo = %q/%q want 夜间折扣/0.50x（取 priority 最高）",
			infos[0].PromoLabel, infos[0].PromoCredits)
	}
	if infos[0].PromoFactor == nil || *infos[0].PromoFactor != 0.5 {
		t.Errorf("glm-5.2 PromoFactor=%v want 0.5", infos[0].PromoFactor)
	}
	// 0 是「限时免费」，必须落成非 nil 指针（nil 会被 /v1/models 当成「无折扣」省略）。
	if infos[1].PromoFactor == nil || *infos[1].PromoFactor != 0 {
		t.Errorf("hy3 PromoFactor=%v want 非 nil 的 0（限时免费）", infos[1].PromoFactor)
	}
	if infos[1].PromoLabel != "限时免费" || infos[1].PromoCredits != "0x" {
		t.Errorf("hy3 promo = %q/%q", infos[1].PromoLabel, infos[1].PromoCredits)
	}
	// 目录外/无优惠条目一字不动，牌价不被覆盖。
	if infos[2].PromoFactor != nil || infos[2].PromoLabel != "" || infos[2].PromoCredits != "" {
		t.Errorf("paid-one 不该有 promo 字段: %+v", infos[2])
	}
	for _, mi := range infos {
		if mi.Credits != "x0.79" {
			t.Errorf("%s 的牌价 credits 被覆盖成 %q（promo 只增字段不改牌价）", mi.ID, mi.Credits)
		}
	}
}

// TestApplyModelPromotionsBadgeOnly 只有标签没有 discount 的条目（错峰使用类）：
// PromoFactor 保持 nil、PromoCredits 保持空，但标签照挂。
func TestApplyModelPromotionsBadgeOnly(t *testing.T) {
	raw := `[{"enabled":true,"priority":1,"modelIds":["hy4-preview"],"badge":{"label":"错峰使用"}}]`
	var promos []v3ModelPromotion
	if err := json.Unmarshal([]byte(raw), &promos); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	infos := promoTargets("hy4-preview")
	applyModelPromotions(infos, promos, promoAt(12, 0))
	if infos[0].PromoFactor != nil {
		t.Errorf("无 discount 时 PromoFactor 必须 nil，got %v", *infos[0].PromoFactor)
	}
	if infos[0].PromoCredits != "" {
		t.Errorf("无 discount 时 PromoCredits 必须空，got %q", infos[0].PromoCredits)
	}
	if infos[0].PromoLabel != "错峰使用" {
		t.Errorf("只有标签的优惠也要挂，got %q", infos[0].PromoLabel)
	}
}

// TestApplyModelPromotionsSkipsInactive 不生效的条目（disabled / 窗口外）不挂任何
// 字段——否则客户端会把白天的牌价显示成夜间折扣。
func TestApplyModelPromotionsSkipsInactive(t *testing.T) {
	raw := `[
		{"enabled":false,"priority":100,"modelIds":["a"],"badge":{"label":"已下线"},
		 "discount":{"discountedCredits":"0x","factor":0}},
		{"enabled":true,"priority":100,"modelIds":["b"],"badge":{"label":"夜间"},
		 "schedule":{"daily":[{"start":"23:00","end":"7:00"}]},
		 "discount":{"discountedCredits":"0.5x","factor":0.5}}
	]`
	var promos []v3ModelPromotion
	if err := json.Unmarshal([]byte(raw), &promos); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	infos := promoTargets("a", "b")
	applyModelPromotions(infos, promos, promoAt(12, 0))
	for _, mi := range infos {
		if mi.PromoFactor != nil || mi.PromoLabel != "" {
			t.Errorf("%s 不该挂优惠: %+v", mi.ID, mi)
		}
	}
}

// TestApplyModelPromotionsOutsideCatalog 优惠表里提到但目录里没有的模型不挂（如同名
// global 变体），也不能 panic 或错挂到别的条目上。
func TestApplyModelPromotionsOutsideCatalog(t *testing.T) {
	raw := `[{"enabled":true,"priority":1,"modelIds":["not-in-catalog"],"badge":{"label":"限时免费"},
		"discount":{"discountedCredits":"0x","factor":0}}]`
	var promos []v3ModelPromotion
	if err := json.Unmarshal([]byte(raw), &promos); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	infos := promoTargets("hy3")
	applyModelPromotions(infos, promos, promoAt(12, 0))
	if infos[0].PromoLabel != "" || infos[0].PromoFactor != nil {
		t.Errorf("目录外条目不该挂到 hy3 上: %+v", infos[0])
	}
}

// TestParseV3Promotions 从完整 /v3/config 响应里取 modelPromotions；坏 JSON /
// 窄表形态一律返回 nil（优惠是展示增强，读不到不能拖垮目录解析）。
func TestParseV3Promotions(t *testing.T) {
	raw := []byte(`{"code":0,"data":{"models":[{"id":"hy3"}],
		"modelPromotions":[{"enabled":true,"priority":1,"modelIds":["hy3"],
			"badge":{"label":"限时免费"},"discount":{"discountedCredits":"0x","factor":0}}]}}`)
	got := parseV3Promotions(raw)
	if len(got) != 1 || got[0].Badge == nil || got[0].Badge.Label != "限时免费" {
		t.Fatalf("parseV3Promotions=%+v want 一条带「限时免费」的优惠", got)
	}
	if got[0].Discount == nil || got[0].Discount.Factor != 0 || got[0].Discount.DiscountedCredits != "0x" {
		t.Errorf("discount 解析错: %+v", got[0].Discount)
	}
	for _, bad := range []string{`{`, `{"code":0,"data":["hy3"]}`, `{"code":0,"data":{}}`} {
		if p := parseV3Promotions([]byte(bad)); len(p) != 0 {
			t.Errorf("%s 不该解析出优惠: %+v", bad, p)
		}
	}
}

// TestParseGlobalModelNamesAppliesPromotions 目录解析本身要挂上优惠（CN FetchModels
// 与 global 探测共用这条路径，两处各写一遍必然漂移）。
func TestParseGlobalModelNamesAppliesPromotions(t *testing.T) {
	raw := []byte(`{"code":0,"data":{"models":[
		{"id":"hy3","credits":"x0.79","maxInputTokens":1000,"maxOutputTokens":500},
		{"id":"glm-5.2","credits":"x0.5","maxInputTokens":1000,"maxOutputTokens":500}
	],"modelPromotions":[
		{"enabled":true,"priority":10,"modelIds":["hy3"],"badge":{"label":"限时免费"},
		 "discount":{"discountedCredits":"0x","factor":0}}
	]}}`)
	names, infos, _, _, err := parseGlobalModelNames(raw)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if len(names) != 2 || len(infos) != 2 {
		t.Fatalf("names=%v infos=%d", names, len(infos))
	}
	if infos[0].ID != "hy3" || infos[0].PromoFactor == nil || *infos[0].PromoFactor != 0 {
		t.Errorf("hy3 应带 factor=0 的优惠: %+v", infos[0])
	}
	if infos[0].Credits != "x0.79" {
		t.Errorf("牌价被改: %q", infos[0].Credits)
	}
	if infos[1].PromoFactor != nil || infos[1].PromoLabel != "" {
		t.Errorf("glm-5.2 无优惠不该挂: %+v", infos[1])
	}
}
