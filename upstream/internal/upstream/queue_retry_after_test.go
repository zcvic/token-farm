package upstream

import (
	"testing"
	"time"
)

// TestParseQueueRetryAfter 排队/容量信封的 body 侧等待口径解析（6020/6021 的
// data.retry_after 与 11134 的 retry_policy.retry_after）。头侧由 ParseRetryAfter
// 负责，两者互不替代：上游这几族只给 body。
func TestParseQueueRetryAfter(t *testing.T) {
	cases := []struct {
		name string
		body string
		want time.Duration
		ok   bool
	}{
		{"6020 真机形态", `{"code":6020,"msg":"queue.waiting.title","requestId":"01a181ca3b67934d01c3f64599c8490c","data":{"queue_position":2502,"queue_size":"999+","retry_after":40,"estimated_wait":12510}}`, 40 * time.Second, true},
		{"retry_after=0（不该出现的边界）", `{"code":6020,"data":{"retry_after":0}}`, 0, false},
		{"6021 无 data（queue.full 无等待口径）", `{"code":6021,"msg":"queue.full.title","requestId":"b1bc87ea"}`, 0, false},
		// 11134 供应商过载：口径在 retry_policy.retry_after（issue #188 生产实测形态）。
		{"11134 retry_policy 形态", `{"code":11134,"msg":"the model provider is temporarily unavailable, please retry later or switch to another model","requestId":"01a1b4d1b3f45f4b99c406eb04d68909","retry_policy":{"retry":true,"retry_after":8}}`, 8 * time.Second, true},
		{"11134 retry_policy 无 retry_after", `{"code":11134,"msg":"model unavailable","retry_policy":{"retry":true}}`, 0, false},
		// 两种形态同时出现时 data 优先（6020 排队口径比 provider 过载更具体）。
		{"data 优先于 retry_policy", `{"code":6020,"data":{"retry_after":40},"retry_policy":{"retry_after":8}}`, 40 * time.Second, true},
		// 异常大值封顶到 retryAfterSanity（防上游口径漂移时把天文数字带回）。
		{"超上限封顶", `{"code":11134,"retry_policy":{"retry_after":999999}}`, retryAfterSanity, true},
		{"非 JSON", `upstream unavailable`, 0, false},
		{"空 body", ``, 0, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			got, ok := parseQueueRetryAfter([]byte(c.body))
			if ok != c.ok || got != c.want {
				t.Errorf("parseQueueRetryAfter(%q)=(%v,%v) want (%v,%v)", c.body, got, ok, c.want, c.ok)
			}
		})
	}
}

// TestParseQueueRetryAfterIgnoresNumericFields 只认结构化 retry_after 字段：
// requestId / msg / queue_position / estimated_wait 里恰好含数字时不得误伤。
func TestParseQueueRetryAfterIgnoresNumericFields(t *testing.T) {
	// requestId 里含 8，msg 里含 40，queue_position 是 2502 —— 都不该被当成等待时长。
	body := `{"code":6021,"msg":"retry in 40s","requestId":"01a1b4d1b3f45f4b99c406eb04d68909",` +
		`"data":{"queue_position":2502,"estimated_wait":12510}}`
	if d, ok := parseQueueRetryAfter([]byte(body)); ok {
		t.Errorf("无 retry_after 字段时不该解析出时长，got %v", d)
	}
}
