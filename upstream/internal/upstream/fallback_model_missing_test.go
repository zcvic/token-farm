package upstream

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

// issue #245：/v2 判渠道校验、回退 /console 之后拿到的 11102 不能升级成 model_blocked。
//
// 现场：15 个号跑几小时后全部软冷却、所有请求 503，日志里 11-128 与 11102 混在一起。
// 11102 走 BlockModelBackoff（负缓存 TTL 2 分钟起、封顶 10 分钟），于是「换接口重试」
// 这一步的净效果变成「把账号锁住」——几个号轮转过去，整池就红了。
//
// 回退路径上的 11102 只说明「这条路径没有这个模型」：我们是因为 /v2 判了渠道校验才
// 换过去的，/console 本来就可能不提供这个模型，这不构成「该账号缺这个模型」的证据。
func TestConsoleFallbackModelMissingIsRequestLevel(t *testing.T) {
	var paths []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths = append(paths, r.URL.Path)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		if strings.Contains(r.URL.Path, "/console/chat/completions") {
			_, _ = w.Write([]byte(`{"code":11102,"msg":"model [deepseek-v4.1-flash] service info not found"}`))
			return
		}
		_, _ = w.Write([]byte(channelBody))
	}))
	defer srv.Close()

	c := New()
	c.ChatBaseCN = srv.URL
	acct := &auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}
	_, status, raw, err := c.ChatStream(acct,
		[]byte(`{"model":"deepseek-v4.1-flash","messages":[{"role":"user","content":"hi"}]}`), "", ChatMeta{})
	if status != http.StatusBadRequest {
		t.Fatalf("status=%d body=%s", status, raw)
	}
	ue, ok := err.(*Error)
	if !ok {
		t.Fatalf("应返回分类信封，got %T (%v)", err, err)
	}
	if ue.Kind != ErrAccountFault || string(raw) != channelBody {
		t.Fatalf("应保留主路径的渠道拒绝，不让备用路径缺模型覆盖原始原因，got %v raw=%s", ue.Kind, raw)
	}
	if len(paths) != 2 || !strings.Contains(paths[0], "/v2/") || !strings.Contains(paths[1], "/console/") {
		t.Fatalf("应当先在 /v2 被拒再换 /console，实际路径=%v", paths)
	}
}

// 主路径自己报 11102 是「模型 id 上游不存在」的请求级答复（issue #280）：不轮转、
// 不写 (账号,模型) 负缓存。回退路径（/console）上的 11102 另按 ErrFallbackModelMissing
// 处理，两者都不是「该账号缺这个模型」。
func TestPrimaryPathModelMissingIsRequestLevel(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadRequest)
		_, _ = w.Write([]byte(`{"code":11102,"msg":"model [glm-5.2] service info not found"}`))
	}))
	defer srv.Close()

	c := New()
	c.ChatBaseCN = srv.URL
	acct := &auth.Auth{UID: "u1", AccessToken: "at", ExpiresAt: 9999999999}
	_, _, _, err := c.ChatStream(acct,
		[]byte(`{"model":"glm-5.2","messages":[{"role":"user","content":"hi"}]}`), "", ChatMeta{})
	ue, ok := err.(*Error)
	if !ok {
		t.Fatalf("应返回分类信封，got %T (%v)", err, err)
	}
	if ue.Kind != ErrModelMissing {
		t.Fatalf("主路径的 11102 应归请求级 ErrModelMissing，got %v", ue.Kind)
	}
}
