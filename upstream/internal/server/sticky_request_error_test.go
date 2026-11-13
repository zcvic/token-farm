package server

import (
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/session"
)

// 请求级错误（内容拦截 / prompt 超长 / 图片无效 / 工具不匹配 / 空消息 / 参数错）不能
// 解绑会话粘性。
//
// 线上现象：报错一次之后缓存就一直不生效。原因是这些错误与账号无关（同一个 body
// 换任何号都会撞），但此前走的是统一的 fail()，它会把会话解绑；下一轮客户端重试时
// 被分配到另一个号上，十万~几十万 token 的前缀从头重算。上游 content_blocked 成片
// 出现时更会连环换号，每一跳都是全量 miss（首字延迟从 3~4s 涨到 10~20s）。
//
// 断言方式：用记录绑定表的假 Store（newBindStore）直接看「解绑有没有写进存储」，
// 而不是只看内存里的 Resolve——后者在账号都被租用时本来就可能为空，测不出差异。
func TestRequestLevelErrorKeepsSessionBinding(t *testing.T) {
	cases := []struct {
		name    string
		status  int
		body    string
		wantMsg string
	}{
		{
			name:   "内容拦截 11-128 unapproved channel",
			status: 400,
			body:   `{"code":"11-128","msg":"Illegal API invocation from an unapproved channel"}`,
			// 这条是 contentBlockedRule 命中的内容拦截（不是渠道校验——后者要码 +
			// 措辞同时成立），属请求级错误：保持绑定。
		},
		{
			name:    "prompt 超长 11115",
			status:  400,
			body:    `{"code":11115,"msg":"prompt is too long"}`,
			wantMsg: "prompt is too long",
		},
		{
			name:    "图片无效",
			status:  400,
			body:    `{"code":11135,"msg":"Parse message failed: invalid image_url content"}`,
			wantMsg: "invalid image_url",
		},
		{
			name:    "工具与结果不匹配 11148",
			status:  400,
			body:    `{"code":11148,"msg":"tool calls and tool results do not match"}`,
			wantMsg: "tool results do not match",
		},
		{
			name:    "空消息 11151",
			status:  400,
			body:    `{"code":11151,"msg":"a message has empty content"}`,
			wantMsg: "empty content",
		},
		{
			name:    "参数拒绝 11133",
			status:  400,
			body:    `{"code":11133,"msg":"Invalid request parameters"}`,
			wantMsg: "Invalid request parameters",
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			const reqBody = `{"model":"cn:deepseek-v4.1-flash","conversation_id":"conv-keep",` +
				`"messages":[{"role":"user","content":"hi"}]}`
			key := session.ExtractKey([]byte(reqBody))
			if key == "" {
				t.Fatal("测试前提失败：body 应能提取出会话键")
			}

			st := newBindStore()
			sess := session.New(session.Config{
				TTL:       time.Minute,
				Store:     st,
				Available: func() []string { return []string{"a", "b"} },
			})
			p := testPoolWith(
				&auth.Auth{UID: "a", AccessToken: "at-a", ExpiresAt: 9999999999},
				&auth.Auth{UID: "b", AccessToken: "at-b", ExpiresAt: 9999999999},
			)
			up := newFakeUpstream(t, func(authz string) (int, string, bool) {
				return c.status, c.body, false
			})
			h := NewHandler(Config{
				Pool: p, Upstream: up, Session: sess,
				SoftCooldown: time.Minute, PromptMode: "passthrough",
			})

			// 会话已经粘在 a 上（那个号上的前缀缓存是热的）。
			sess.Bind(key, "a")
			if uid, ok := st.LoadBinds()[key]; !ok || uid != "a" {
				t.Fatalf("前置绑定没写进存储: %v", st.LoadBinds())
			}

			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(reqBody)))
			if rec.Code != 400 {
				t.Fatalf("请求级错误应回 400，实际 %d body=%s", rec.Code, rec.Body)
			}
			if c.wantMsg != "" && !strings.Contains(rec.Body.String(), c.wantMsg) {
				t.Fatalf("应透传上游原文（含 %q）:\n%s", c.wantMsg, rec.Body.String())
			}

			binds := st.LoadBinds()
			if uid, ok := binds[key]; !ok || uid != "a" {
				t.Fatalf("请求级错误不该解绑会话（客户端重试会落到冷号上全量重算）：binds=%v", binds)
			}
		})
	}
}

// TestAccountLevelErrorUnbindsSession 反向锚点：账号级故障**必须**解绑（否则会一直
// 钉在坏号上反复撞墙）。用 429 软限流做样本——它明确是账号级信号。
func TestAccountLevelErrorUnbindsSession(t *testing.T) {
	const reqBody = `{"model":"cn:deepseek-v4.1-flash","conversation_id":"conv-drop",` +
		`"messages":[{"role":"user","content":"hi"}]}`
	key := session.ExtractKey([]byte(reqBody))

	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"a", "b"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "a", AccessToken: "at-a", ExpiresAt: 9999999999},
		&auth.Auth{UID: "b", AccessToken: "at-b", ExpiresAt: 9999999999},
	)
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		return 429, `{"code":6004,"msg":"rate limit"}`, false
	})
	h := NewHandler(Config{Pool: p, Upstream: up, Session: sess, SoftCooldown: time.Minute})

	sess.Bind(key, "a")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(reqBody)))

	if _, ok := st.LoadBinds()[key]; ok {
		t.Fatalf("账号级故障（429）应解绑会话，实际仍绑着: %v", st.LoadBinds())
	}
}

// TestChannelCheckStillUnbindsSession 渠道校验（11-128）保持解绑：它按**账号**灰度
// 启用，解绑后下一轮有机会换到未被灰度的号，钉在原号上只会反复被拒。
func TestChannelCheckStillUnbindsSession(t *testing.T) {
	const reqBody = `{"model":"cn:deepseek-v4.1-flash","conversation_id":"conv-ch",` +
		`"messages":[{"role":"user","content":"hi"}]}`
	key := session.ExtractKey([]byte(reqBody))

	st := newBindStore()
	sess := session.New(session.Config{
		TTL:       time.Minute,
		Store:     st,
		Available: func() []string { return []string{"a", "b"} },
	})
	p := testPoolWith(
		&auth.Auth{UID: "a", AccessToken: "at-a", ExpiresAt: 9999999999},
		&auth.Auth{UID: "b", AccessToken: "at-b", ExpiresAt: 9999999999},
	)
	up := newFakeUpstream(t, func(authz string) (int, string, bool) {
		// 渠道校验要**码 + 措辞**同时成立（IsChannelCheck）：只给 11-128 会被归到
		// 内容拦截（ErrContentBlocked），那是请求级错误、保持绑定。
		return 400, `{"code":11128,"msg":"request illegal","requestId":"x",` +
			`"displayTips":{"zh":"这次调用没通过渠道校验，可能不是官方客户端发出的。",` +
			`"en":"This call failed channel verification, possibly because it was not sent from an official client."}}`, false
	})
	h := NewHandler(Config{Pool: p, Upstream: up, Session: sess, SoftCooldown: time.Minute})

	sess.Bind(key, "a")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("POST", "/v1/chat/completions", strings.NewReader(reqBody)))

	if _, ok := st.LoadBinds()[key]; ok {
		t.Fatalf("渠道校验应解绑（换到未被灰度的号），实际仍绑着: %v", st.LoadBinds())
	}
}
