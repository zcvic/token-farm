package server

import (
	"testing"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/upstream"
)

// /v1/models 要透出限时优惠字段（issue #203）。
//
// 背景：`credits` 是上游**牌价**倍率，`promo_*` 才是当前生效折扣（WorkBuddy 客户端
// 显示的就是生效价）。这组字段此前只在面板的 /panel/api/models 里有，OpenAI 兼容
// 端点看不到——第三方启动器只读 /v1/models，于是免费模型显示成收费、或者显示旧牌价。
//
// 判据：
//   - PromoFactor 非 nil（上游给了可计算的折扣系数）→ 写 promo_factor + promo_credits；
//   - PromoLabel 非空 → 写 promo_label（「错峰使用」这类只有标签没有系数的也要透出）；
//   - 都没有 → 一个都不写（不编造「原价」）。
//
// 变异判据：删掉 handler.go 里那段 promo 透出，本用例红。
func TestV1ModelsExposesPromoFields(t *testing.T) {
	f := 0.0
	entry := applyModelInfoFields(map[string]any{"id": "cn:free-one"}, upstream.ModelInfo{
		ID:           "free-one",
		Credits:      "0.79",
		PromoFactor:  &f, // 0 = 限时免费
		PromoCredits: "0x",
		PromoLabel:   "限时免费",
	})
	got, ok := entry["promo_factor"]
	if !ok {
		t.Fatalf("promo_factor 缺失（限时免费必须能被读出来，不能因为 0 值被省略）：%v", entry)
	}
	if got != 0.0 {
		t.Fatalf("promo_factor = %v want 0", got)
	}
	if got, _ := entry["promo_credits"].(string); got != "0x" {
		t.Fatalf("promo_credits = %q want \"0x\"", got)
	}
	if got, _ := entry["promo_label"].(string); got != "限时免费" {
		t.Fatalf("promo_label = %q want 限时免费", got)
	}
	// 牌价原样保留：启动器要拿它划掉显示。
	if got, _ := entry["credits"].(string); got != "0.79" {
		t.Fatalf("credits = %q want 0.79（牌价不能被折扣覆盖）", got)
	}
}

// TestV1ModelsPromoLabelOnly 只有标签、没有系数（错峰使用）：promo_factor /
// promo_credits 不写，标签照写。
func TestV1ModelsPromoLabelOnly(t *testing.T) {
	entry := applyModelInfoFields(map[string]any{"id": "cn:peak"}, upstream.ModelInfo{
		ID: "peak", PromoLabel: "错峰使用",
	})
	if _, ok := entry["promo_factor"]; ok {
		t.Fatalf("没有折扣系数时不该写 promo_factor：%v", entry)
	}
	if _, ok := entry["promo_credits"]; ok {
		t.Fatalf("没有折扣系数时不该写 promo_credits：%v", entry)
	}
	if got, _ := entry["promo_label"].(string); got != "错峰使用" {
		t.Fatalf("只带标签的优惠也要透出：%q", got)
	}
}

// TestV1ModelsPromoAbsent 完全没优惠：三个字段都不出现（别让客户端误判成「免费」）。
func TestV1ModelsPromoAbsent(t *testing.T) {
	plain := applyModelInfoFields(map[string]any{"id": "cn:paid"}, upstream.ModelInfo{ID: "paid", Credits: "x1.0"})
	for _, k := range []string{"promo_factor", "promo_credits", "promo_label"} {
		if _, ok := plain[k]; ok {
			t.Fatalf("无优惠的模型不该出现 %s：%v", k, plain)
		}
	}
}

// TestModelListEndToEndPromo 端到端：上游 /v3/config 带 modelPromotions 时，
// /v1/models 的条目要带上 promo 三字段；牌价 credits 不被覆盖。
func TestModelListEndToEndPromo(t *testing.T) {
	body := `{"code":0,"data":{"models":[
		{"id":"hy3","name":"Hy3","credits":"x0.79","maxInputTokens":192000,"maxOutputTokens":64000},
		{"id":"glm-5.2","name":"GLM 5.2","credits":"x0.5","maxInputTokens":128000,"maxOutputTokens":32000}
	],"agents":[{"name":"cli","models":["hy3","glm-5.2"]}],
	"modelPromotions":[
		{"enabled":true,"priority":10,"modelIds":["hy3"],"badge":{"label":"限时免费"},
		 "discount":{"discountedCredits":"0x","factor":0}},
		{"enabled":true,"priority":20,"modelIds":["glm-5.2"],"badge":{"label":"错峰使用"}}
	]}}`
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		return 200, body, false
	})
	resetModelsCache()
	p := testPoolWith(&auth.Auth{UID: "u1", AccessToken: "at1", ExpiresAt: 9999999999})
	h := NewHandler(Config{Pool: p, Upstream: up, GlobalEnabled: false})

	var free, labelOnly map[string]any
	for _, m := range h.modelList() {
		switch m["id"] {
		case "cn:hy3":
			free = m
		case "cn:glm-5.2":
			labelOnly = m
		}
	}
	if free == nil || labelOnly == nil {
		t.Fatalf("条目缺失：free=%v labelOnly=%v", free, labelOnly)
	}
	if got, ok := free["promo_factor"]; !ok || got != 0.0 {
		t.Errorf("cn:hy3 promo_factor=%v (存在=%v) want 0（限时免费）", got, ok)
	}
	if got, _ := free["promo_credits"].(string); got != "0x" {
		t.Errorf("cn:hy3 promo_credits=%q want 0x", got)
	}
	if got, _ := free["promo_label"].(string); got != "限时免费" {
		t.Errorf("cn:hy3 promo_label=%q", got)
	}
	if got, _ := free["credits"].(string); got != "x0.79" {
		t.Errorf("cn:hy3 牌价被覆盖: %q", got)
	}
	if _, ok := labelOnly["promo_factor"]; ok {
		t.Errorf("cn:glm-5.2 只有标签，不该有 promo_factor: %v", labelOnly)
	}
	if got, _ := labelOnly["promo_label"].(string); got != "错峰使用" {
		t.Errorf("cn:glm-5.2 promo_label=%q want 错峰使用", got)
	}
}
