// timing_test.go 分段耗时观测：nil 出参零开销，非 nil 时 Do 有测量、合计等于两段之和。
package upstream

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

func TestChatTimingObservation(t *testing.T) {
	// 上游故意慢 5ms：Do 的测量必须能看见这段睡眠（时钟粗到 15ms 也够）。
	c := testClient(func(r *http.Request) (*http.Response, error) {
		time.Sleep(5 * time.Millisecond)
		return &http.Response{
			StatusCode: 200,
			Header:     http.Header{"Content-Type": []string{"text/event-stream"}},
			Body:       io.NopCloser(strings.NewReader("data: [DONE]\n\n")),
		}, nil
	})
	var timing ChatTiming
	rc, status, _, err := c.ChatStreamContext(context.Background(),
		&auth.Auth{AccessToken: "at"}, []byte(`{"model":"m"}`), "", ChatMeta{}, &timing)
	if err != nil || status != 200 {
		t.Fatalf("status=%d err=%v", status, err)
	}
	rc.Close()
	// Prep 覆盖 prepareBody 这类纯 CPU 段，耗时可能小于系统时钟分辨率（本仓开发机
	// 实测 time.Since(time.Now()) 就是 0ns），因此只断言不小于零；Do 有 5ms 睡眠打底。
	if timing.Prep < 0 {
		t.Errorf("prep 为负: %v", timing.Prep)
	}
	if timing.Do <= 0 {
		t.Errorf("do 未测量: %v", timing.Do)
	}
	if got := timing.Total(); got != timing.Prep+timing.Do {
		t.Errorf("Total=%v want %v", got, timing.Prep+timing.Do)
	}

	// nil 出参：不 panic、返回值不受影响（既有调用方零行为变化）。
	rc, status, _, err = c.ChatStreamContext(context.Background(),
		&auth.Auth{AccessToken: "at"}, []byte(`{"model":"m"}`), "", ChatMeta{}, nil)
	if err != nil || status != 200 {
		t.Fatalf("nil timing: status=%d err=%v", status, err)
	}
	rc.Close()
}

// 零值表示「未测量」，不是 0ms 的观测。
func TestChatTimingZeroTotal(t *testing.T) {
	if got := (ChatTiming{}).Total(); got != 0 {
		t.Errorf("zero timing Total=%v want 0", got)
	}
}
