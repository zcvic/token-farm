package upstream

import (
	"strings"
	"testing"
)

// carrier 复现 sanitizeMessages 对回流载体的处理顺序：先做通用净化，再洗注入身份指令。
func carrier(in string) string { return scrubInjectedIdentity(sanitizeText(in)) }

// injectedSentence 构造上游服务端注入的身份指令形态（issue #187 逐字取证）：
// 「When asked about your model identity, identify yourself as <name>.」
//
// 用拼接而不是整句字面量：这条规则要匹配的是**句式**，测试也不必再抄一份长句。
func injectedSentence(name string) string {
	return "When asked about your " + "model " + "identity, " + injectedIdentityProbe + " " + name
}

// TestSanitizeInjectedIdentitySentence 上游服务端注入的身份指令整句改写成中性表述。
//
// 它不在出站 body 里（上游在服务端拼），但模型会在 reasoning_content 里复述，
// 客户端把思维链带回历史后下一轮就原样进出站——必须掐断回流。
func TestSanitizeInjectedIdentitySentence(t *testing.T) {
	in := "I should follow the instruction. " + injectedSentence("Tencent Hunyuan Hy.") + " Then answer."
	out := carrier(in)
	if strings.Contains(strings.ToLower(out), injectedIdentityProbe) {
		t.Errorf("注入的身份指令未被改写: %q", out)
	}
	// 断言具体的中性表述（用字面量而不是常量：写成常量的话「替换串改成空串」这种
	// 变异会因 strings.Contains(out, "") 恒真而漏测）。
	if !strings.Contains(out, "answer using only the information") {
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
	in := strings.ToUpper(injectedSentence("Tencent Hunyuan Hy."))
	if out := carrier(in); strings.Contains(strings.ToLower(out), injectedIdentityProbe) {
		t.Errorf("大写变体漏洗: %q", out)
	}
}

// TestSanitizeInjectedIdentityDottedName 身份名自带点（GPT-4.1 / Claude 3.5）时，
// 句尾不能断在第一个点——否则会留下 "1 by OpenAI." 半句。句号后的空白要原样接回。
func TestSanitizeInjectedIdentityDottedName(t *testing.T) {
	in := injectedSentence("GPT-4.1 by OpenAI.") + " Then answer."
	out := carrier(in)
	if strings.Contains(out, "1 by OpenAI.") {
		t.Errorf("身份名被断在第一个点: %q", out)
	}
	if strings.Contains(strings.ToLower(out), injectedIdentityProbe) {
		t.Errorf("带点身份名未被改写: %q", out)
	}
	if !strings.Contains(out, "conversation. Then answer.") {
		t.Errorf("句后空白未接回，残句粘连: %q", out)
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
		if out := carrier(in); out != strings.TrimSpace(in) {
			t.Errorf("误伤合法指令：%q -> %q", in, out)
		}
	}
}

// TestSanitizeModelIdentityTag 去掉 model_identity 标签本身、保留内部文本。
// 只删标签：标签留着会让模型继续把它当「有个身份块」反复提起。
func TestSanitizeModelIdentityTag(t *testing.T) {
	tag := "<" + modelIdentityTagProbe + ">"
	in := tag + "You are WorkBuddy.<" + "/" + modelIdentityTagProbe + "> Then answer."
	out := carrier(in)
	if strings.Contains(strings.ToLower(out), modelIdentityTagProbe) {
		t.Errorf("标签未剥离: %q", out)
	}
	if !strings.Contains(out, "You are WorkBuddy.") {
		t.Errorf("内部文本不该被删: %q", out)
	}
}

// TestSanitizeModelIdentityTagVariants 大小写与单边标签（模型复述时常只留一半）
// 都要剥掉。
func TestSanitizeModelIdentityTagVariants(t *testing.T) {
	open := "<" + modelIdentityTagProbe + ">"
	close := "</" + modelIdentityTagProbe + ">"
	for _, in := range []string{
		open + "x" + close,
		strings.ToUpper(open) + "x" + close,
		open + "x",
	} {
		if out := carrier(in); strings.Contains(strings.ToLower(out), modelIdentityTagProbe) {
			t.Errorf("变体漏洗：%q -> %q", in, out)
		}
	}
}

// TestSanitizeIdentityRunsOnAssistantReasoning 泄漏的载体是 assistant 的
// reasoning_content —— 必须由 sanitizeMessages 覆盖到（不是 identity 门控，
// 那个只对 system/developer 生效）。
func TestSanitizeIdentityRunsOnAssistantReasoning(t *testing.T) {
	obj := decodeMessages(t, `{"messages":[{"role":"assistant","content":"ok",`+
		`"reasoning_content":"`+injectedSentence("Tencent Hunyuan Hy.")+`"}]}`)
	msgs := obj["messages"].([]any)
	if !sanitizeMessages(msgs) {
		t.Fatal("assistant 的注入复述应被判为有改动")
	}
	rc, _ := msgs[0].(map[string]any)["reasoning_content"].(string)
	if strings.Contains(strings.ToLower(rc), injectedIdentityProbe) {
		t.Errorf("assistant reasoning_content 未洗: %q", rc)
	}
}

// TestSanitizeIdentityScopeOnlyCarriers 注入身份指令只洗回流载体：assistant 正文与
// 任意消息的 reasoning_content；system / user 正文里的同形句是用户自己写的人设，
// 改写它等于篡改提示词。
func TestSanitizeIdentityScopeOnlyCarriers(t *testing.T) {
	sentence := injectedSentence("Tencent Hunyuan Hy.")
	obj := decodeMessages(t, `{"messages":[`+
		`{"role":"system","content":"`+sentence+`"},`+
		`{"role":"user","content":"`+sentence+`"},`+
		`{"role":"assistant","content":"`+sentence+`"}]}`)
	msgs := obj["messages"].([]any)
	if !sanitizeMessages(msgs) {
		t.Fatal("assistant 正文的复述应被判为有改动")
	}
	for i := 0; i < 2; i++ { // system / user 原样保留
		got, _ := msgs[i].(map[string]any)["content"].(string)
		if got != sentence {
			t.Errorf("第 %d 条（%v）不该被改写：%q", i, msgs[i].(map[string]any)["role"], got)
		}
	}
	got, _ := msgs[2].(map[string]any)["content"].(string)
	if strings.Contains(strings.ToLower(got), injectedIdentityProbe) {
		t.Errorf("assistant 正文未洗: %q", got)
	}
}
