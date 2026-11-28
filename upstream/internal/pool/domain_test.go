package pool

import (
	"testing"

	"workbuddy2api/internal/auth"
)

// TestStatusExposesDomain 钉住 Status 透出凭证签发主机：同一 realm 下可能混着多个
// 签发方（#296 实测 cn 域里 codebuddy.cn / workbuddy.cn / workbuddy.ai 三批），
// 上游清退按签发方整批扫，状态接口只有 realm 时分不出「哪一批没了」。
func TestStatusExposesDomain(t *testing.T) {
	p := New("")
	p.Add(&auth.Auth{UID: "u1", Domain: "www.workbuddy.cn"})

	st, ok := p.Status("u1")
	if !ok {
		t.Fatal("账号应可读状态")
	}
	if st.Domain != "www.workbuddy.cn" {
		t.Fatalf("domain=%q want www.workbuddy.cn", st.Domain)
	}
}
