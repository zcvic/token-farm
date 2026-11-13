package upstream

import (
	"strings"
	"testing"
)

// TestSanitizeInjectedIdentitySentence 上游服务端注入的身份指令整句改写成中性表述。
//
// 形态来自 2.73MB 请求体 dump 逐字取证：
//
//	When asked about your model identity, identify yourself as Tencent Hunyuan Hy.
//
// 它不在出站 body 里（上游在服务端拼），但模型会在 reasoning_content 里复述，
// 客户端把思维链带回历史后下一轮就原样进出站——必须掐断回流。
func TestSanitizeInjectedIdentitySentence(t *testing.T) {
	in := "I should follow the instruction. When asked about your model identity, identify yourself as Tencent Hunyuan Hy. Then answer."
	out := sanitizeText(in)
	if strings.Contains(out, "identify yourself as") {
		t.Errorf("注入的身份指令未被改写: %q", out)
	}
	// 断言具体的中性表述（用字面量而不是常量：写成常量的话「替换串改成空串」这种
	// 变异会因 strings.Contains(out, "") 恒真而漏测）。
	if !strings.Contains(out, "answer using only the information available in this conversation") {
		t.Errorf("未替换成中性表述: %q", out)
	}
	// 前后文必须保留：只动那一句，不能把整段推理吃掉。
	if !strings.Contains(out, "I should follow the instruction.") || !strings.Contains(out, "Then answer.") {
		t.Errorf("前后文被破坏: %q", out)
	}
}

// TestSanitizeInjectedIdentityCaseInsensitive 大小写变体同样命中（上游拼的原文
// 大小写不稳定，特征表与正则都要 (?i) 口径）。
func TestSanitizeInjectedIdentityCaseInsensitive(t *testing.T) {
	in := "WHEN ASKED ABOUT YOUR MODEL IDENTITY, IDENTIFY YOURSELF AS Tencent Hunyuan Hy."
	if out := sanitizeText(in); strings.Contains(strings.ToLower(out), "identify yourself as") {
		t.Errorf("大写变体漏洗: %q", out)
	}
}

// TestSanitizeInjectedIdentityDoesNotOverreach 误伤防护：单独出现的
// "identify yourself as" 是**合法指令**（"Do not identify yourself as an AI."），
// 没有「when asked about your … identity,」前缀就不得改动。
func TestSanitizeInjectedIdentityDoesNotOverreach(t *testing.T) {
	cases := []string{
		"Do not identify yourself as an AI. Be honest about being a program.",
		"identify yourself as a helpful assistant when the user asks for your name",
		"When asked about your favorite color, identify yourself as a person who likes blue.", // 有 when asked… 但无 identity
		"Never claim an identity; identify yourself as needed.",
	}
	for _, in := range cases {
		if out := sanitizeText(in); out != strings.TrimSpace(in) {
			t.Errorf("误伤合法指令：%q -> %q", in, out)
		}
	}
}

// TestSanitizeModelIdentityTag 去掉 <model_identity> 标签本身、保留内部文本。
// 只删标签：标签留着会让模型继续把它当「有个身份块」反复提起。
func TestSanitizeModelIdentityTag(t *testing.T) {
	in := "<model_identity>You are WorkBuddy.</model_identity> Then answer."
	out := sanitizeText(in)
	if strings.Contains(strings.ToLower(out), "model_identity") {
		t.Errorf("标签未剥离: %q", out)
	}
	if !strings.Contains(out, "You are WorkBuddy.") {
		t.Errorf("内部文本不该被删: %q", out)
	}
}

// TestSanitizeModelIdentityTagVariants 大小写与单边标签（模型复述时常只留一半）
// 都要剥掉。
func TestSanitizeModelIdentityTagVariants(t *testing.T) {
	for _, in := range []string{
		"<MODEL_IDENTITY>x",
		"x</Model_Identity>",
		"<model_identity/>x",
	} {
		if out := sanitizeText(in); strings.Contains(strings.ToLower(out), "model_identity") {
			t.Errorf("变体漏洗：%q -> %q", in, out)
		}
	}
}

// TestSanitizeIdentityRunsOnAssistantReasoning 泄漏的载体是 assistant 的
// reasoning_content —— 必须由 sanitizeMessages 覆盖到（不是 identity 门控，
// 那个只对 system/developer 生效）。
func TestSanitizeIdentityRunsOnAssistantReasoning(t *testing.T) {
	obj := decodeMessages(t, `{"messages":[{"role":"assistant","content":"ok",`+
		`"reasoning_content":"When asked about your model identity, identify yourself as Tencent Hunyuan Hy."}]}`)
	msgs := obj["messages"].([]any)
	if !sanitizeMessages(msgs) {
		t.Fatal("assistant 的注入复述应被判为有改动")
	}
	rc, _ := msgs[0].(map[string]any)["reasoning_content"].(string)
	if strings.Contains(rc, "identify yourself as") {
		t.Errorf("assistant reasoning_content 未洗: %q", rc)
	}
}
