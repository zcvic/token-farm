// client_identity_test.go 钉住 5.11.3 桌面端 X-Client-* 三头的隔离口径：
// 只发国内版，国际版一律不发；版本段按 realm 取内置默认，可被 config 覆盖。
//
// 为什么必须有这条测试：两侧分发包不同（CN 5.11.3 / GL 5.9.4），国际版
// app.asar 里没有 proxy-env2 的 registerOutboundHeaderProvider，官方国际客户端
// 从不发这三个头。一旦有人把注入挪进不分 realm 的公共函数，国际链路就会多出
// 一层它不存在的指纹。
package upstream

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"workbuddy2api/internal/auth"
)

func TestClientIdentityCN(t *testing.T) {
	a := &auth.Auth{AccessToken: "at", UID: "u1"}
	c := &Client{}
	req := httptest.NewRequest(http.MethodPost, "https://copilot.tencent.com/v2/report", nil)
	c.CommonHeaders(req, a)
	for hdr, want := range map[string]string{
		"X-Client-Version":  "5.11.3",
		"X-Client-Platform": "windows",
		"X-Client-Product":  "WorkBuddy",
	} {
		if got := req.Header.Get(hdr); got != want {
			t.Errorf("CN %s = %q want %q", hdr, got, want)
		}
	}
}

func TestClientIdentityGlobalAbsent(t *testing.T) {
	a := &auth.Auth{AccessToken: "at", UID: "u1", Domain: "www.workbuddy.ai"}
	c := &Client{}
	req := httptest.NewRequest(http.MethodPost, "https://www.workbuddy.ai/v2/report", nil)
	c.CommonHeaders(req, a)
	for _, hdr := range []string{"X-Client-Version", "X-Client-Platform", "X-Client-Product"} {
		if got := req.Header.Get(hdr); got != "" {
			t.Errorf("global %s = %q want empty（国际版不发三头）", hdr, got)
		}
	}
}

func TestClientIdentityBillingByRealm(t *testing.T) {
	c := &Client{}
	for _, tc := range []struct {
		name    string
		a       *auth.Auth
		present bool
	}{
		{"cn", &auth.Auth{AccessToken: "at", UID: "u1"}, true},
		{"global", &auth.Auth{AccessToken: "at", UID: "u1", Domain: "www.workbuddy.ai"}, false},
	} {
		req := httptest.NewRequest(http.MethodGet, "https://billing.example/v2/billing/meter/x", nil)
		c.BillingHeaders(req, tc.a)
		got := req.Header.Get("X-Client-Platform")
		if tc.present && got != "windows" {
			t.Errorf("%s billing X-Client-Platform = %q want windows", tc.name, got)
		}
		if !tc.present && got != "" {
			t.Errorf("%s billing X-Client-Platform = %q want empty", tc.name, got)
		}
	}
}

func TestClientIdentityFollowsClientVersion(t *testing.T) {
	a := &auth.Auth{AccessToken: "at", UID: "u1"}
	c := &Client{ClientVersion: "6.0.0"}
	req := httptest.NewRequest(http.MethodPost, "https://copilot.tencent.com/v2/report", nil)
	c.CommonHeaders(req, a)
	if got := req.Header.Get("X-Client-Version"); got != "6.0.0" {
		t.Errorf("X-Client-Version = %q want 6.0.0（config client_version 覆盖）", got)
	}
}

// TestDropClientIdentityHeaders 小程序口径必须撤掉桌面三头，避免发出
// `5.11.3 / miniprogram / WorkBuddy` 这种官方不存在的第四种组合。
func TestDropClientIdentityHeaders(t *testing.T) {
	a := &auth.Auth{AccessToken: "at", UID: "u1"}
	c := &Client{}
	req := httptest.NewRequest(http.MethodPost, "https://copilot.tencent.com/v2/report", nil)
	c.BillingHeaders(req, a)
	c.dropClientIdentityHeaders(req)
	for _, hdr := range []string{"X-Client-Version", "X-Client-Platform", "X-Client-Product"} {
		if got := req.Header.Get(hdr); got != "" {
			t.Errorf("%s = %q want empty（小程序口径撤头）", hdr, got)
		}
	}
}

// TestRPCUAForRealmShape RPC 层 UA 首段即品牌 slug（global 为 workbuddy-ai），
// chat 层首段恒驼峰 WorkBuddy；两条链路不能混成一个 UA。
func TestRPCUAForRealmShape(t *testing.T) {
	c := &Client{}
	cn := &auth.Auth{}
	gl := &auth.Auth{Domain: "www.workbuddy.ai"}
	if got, want := c.rpcUAFor(cn), "WorkBuddy/5.11.3 WorkBuddy/5.11.3 CLI/2.176.0"; got != want {
		t.Errorf("rpcUAFor(cn) = %q want %q", got, want)
	}
	if got, want := c.rpcUAFor(gl), "workbuddy-ai/5.9.4 workbuddy-ai/5.9.4 CLI/2.165.0"; got != want {
		t.Errorf("rpcUAFor(global) = %q want %q", got, want)
	}
	if got, want := c.userAgent(gl), "WorkBuddy/5.9.4 workbuddy-ai/5.9.4 CLI/2.165.0"; got != want {
		t.Errorf("userAgent(global) = %q want %q", got, want)
	}
}
