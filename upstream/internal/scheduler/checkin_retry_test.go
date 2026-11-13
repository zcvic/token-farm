package scheduler

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/pool"
	"workbuddy2api/internal/upstream"
)

// newLateRetryS 构造一个签到上游：前 failN 次返回 500+10001（瞬时失败），之后成功。
// 余额查询恒成功，避免 fail_resource 干扰断言。
func newLateRetryS(t *testing.T, failN int32, calls *atomic.Int32) (*Scheduler, *pool.Pool) {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/daily-checkin"):
			if calls.Add(1) <= failN {
				w.WriteHeader(500)
				w.Write([]byte(`{"code":10001,"msg":"签到失败，请稍后重试","requestId":"x"}`))
				return
			}
			w.Write([]byte(`{"code":0,"msg":"ok","data":{}}`))
		case strings.HasSuffix(r.URL.Path, "/get-user-resource"):
			w.Write([]byte(`{"code":0,"data":{"Response":{"Data":{"Accounts":[{"CycleCapacitySize":1000,"CycleCapacityRemain":500,"CycleCapacityUsed":0}]}}}}`))
		default:
			http.Error(w, "not found", 404)
		}
	}))
	t.Cleanup(srv.Close)
	p := pool.New("")
	p.Add(&auth.Auth{UID: "u1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	return New(Config{Pool: p, Upstream: up}), p
}

// setLateRetryDelays 把补签节奏压到毫秒级并还原。
func setLateRetryDelays(t *testing.T, ds ...time.Duration) {
	t.Helper()
	old := checkinLateRetryDelays
	checkinLateRetryDelays = ds
	t.Cleanup(func() { checkinLateRetryDelays = old })
}

// waitFor 轮询等待条件成立（补签在后台 goroutine 里跑，没有同步点）。
func waitFor(t *testing.T, d time.Duration, cond func() bool) bool {
	t.Helper()
	deadline := time.Now().Add(d)
	for time.Now().Before(deadline) {
		if cond() {
			return true
		}
		time.Sleep(time.Millisecond)
	}
	return cond()
}

// TestCheckinLateRetrySucceedsAfterTransient 主批秒级退避仍失败 → 排进分钟级补签，
// 补签成功并记账（CheckinDoneToday 为真）。
func TestCheckinLateRetrySucceedsAfterTransient(t *testing.T) {
	var calls atomic.Int32
	s, p := newLateRetryS(t, 1, &calls) // 首次失败，第一次补签即成功
	setLateRetryDelays(t, 2*time.Millisecond, 5*time.Millisecond)

	out, err := s.CheckinAll()
	if err != nil {
		t.Fatalf("err=%v", err)
	}
	if len(out) != 1 || out[0].Status != CheckinFail {
		t.Fatalf("主批 out=%+v want fail（瞬时失败）", out)
	}
	if p.CheckinDoneToday("u1") {
		t.Fatal("主批失败后不该有已签记账")
	}
	if !waitFor(t, 2*time.Second, func() bool { return p.CheckinDoneToday("u1") }) {
		t.Fatalf("补签未成功：calls=%d（应在 2ms 后的第一轮补签成功）", calls.Load())
	}
	if got := calls.Load(); got < 2 {
		t.Errorf("daily-checkin calls=%d want >=2（主批 1 次 + 补签 1 次）", got)
	}
}

// TestLateRetryWaitIsAbsoluteOffset 补签延迟是「从本批结束起算的绝对偏移」：
// 第二轮只再等 40m-10m=30m，不是再等 40m（否则第二次补签被推到 50 分钟后）。
func TestLateRetryWaitIsAbsoluteOffset(t *testing.T) {
	old := checkinLateRetryDelays
	checkinLateRetryDelays = []time.Duration{10 * time.Minute, 40 * time.Minute}
	t.Cleanup(func() { checkinLateRetryDelays = old })

	if got := lateRetryWait(0, 0); got != 10*time.Minute {
		t.Errorf("第一轮 wait=%v want 10m", got)
	}
	if got := lateRetryWait(1, 10*time.Minute); got != 30*time.Minute {
		t.Errorf("第二轮 wait=%v want 30m（40m 绝对偏移 − 已等 10m）", got)
	}
}

// TestCheckinLateRetryTwoRounds 补签是「两轮绝对偏移」：第一轮仍失败的要留到第二轮
// 再试（10m 覆盖结算抖动、40m 覆盖上游慢），第二轮成功即记账。
func TestCheckinLateRetryTwoRounds(t *testing.T) {
	var calls atomic.Int32
	s, p := newLateRetryS(t, 2, &calls) // 主批 + 第一轮补签失败，第二轮成功
	setLateRetryDelays(t, 2*time.Millisecond, 5*time.Millisecond)

	if out, _ := s.CheckinAll(); len(out) != 1 || out[0].Status != CheckinFail {
		t.Fatalf("主批 out=%+v want fail", out)
	}
	if !waitFor(t, 2*time.Second, func() bool { return p.CheckinDoneToday("u1") }) {
		t.Fatalf("第二轮补签未成功：calls=%d want 3", calls.Load())
	}
	if got := calls.Load(); got != 3 {
		t.Errorf("daily-checkin calls=%d want 3（主批 1 + 两轮补签各 1）", got)
	}
}

// TestCheckinLateRetryStopsAfterLastRound 两轮之后仍失败就放弃，落 WARN 等下一个
// 签到整点——不能无限重试（每轮都是真实写调用）。
func TestCheckinLateRetryStopsAfterLastRound(t *testing.T) {
	var calls atomic.Int32
	s, p := newLateRetryS(t, 1000, &calls) // 永远失败
	setLateRetryDelays(t, 2*time.Millisecond, 5*time.Millisecond)

	if out, _ := s.CheckinAll(); len(out) != 1 || out[0].Status != CheckinFail {
		t.Fatalf("主批 out=%+v want fail", out)
	}
	time.Sleep(200 * time.Millisecond)
	if got := calls.Load(); got != 3 {
		t.Errorf("daily-checkin calls=%d want 3（主批 1 + 补签 2 轮，之后放弃）", got)
	}
	if p.CheckinDoneToday("u1") {
		t.Error("全失败不该有已签记账")
	}
}

// TestCheckinLateRetrySkipsNonRetryable 业务拒绝（400/10000）不排补签：重试必然
// 同样失败，排进去只是每天多打几轮无效写调用。
func TestCheckinLateRetrySkipsNonRetryable(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/daily-checkin"):
			calls.Add(1)
			w.WriteHeader(400)
			w.Write([]byte(`{"code":10000,"msg":"参数错误"}`))
		case strings.HasSuffix(r.URL.Path, "/get-user-resource"):
			w.Write([]byte(`{"code":0,"data":{"Response":{"Data":{"Accounts":[{"CycleCapacitySize":1000,"CycleCapacityRemain":500,"CycleCapacityUsed":0}]}}}}`))
		default:
			http.Error(w, "not found", 404)
		}
	}))
	defer srv.Close()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "u1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{Pool: p, Upstream: up})
	setLateRetryDelays(t, 2*time.Millisecond, 5*time.Millisecond)

	if out, _ := s.CheckinAll(); len(out) != 1 || out[0].Status != CheckinFail {
		t.Fatalf("out=%+v want fail", out)
	}
	// 给两轮补签足够的时间窗：若误排，calls 会涨。
	time.Sleep(60 * time.Millisecond)
	if got := calls.Load(); got != 1 {
		t.Errorf("daily-checkin calls=%d want 1（业务拒绝不排补签）", got)
	}
}

// TestCheckinLateRetrySkipsAlreadyDone 补签前先查本地记账：已签（用户自己在手机上
// 签了、或上一轮补签成功）就不再打上游写接口。
func TestCheckinLateRetrySkipsAlreadyDone(t *testing.T) {
	var calls atomic.Int32
	s, p := newLateRetryS(t, 0, &calls)
	p.NoteCheckinDone("u1")
	setLateRetryDelays(t, time.Millisecond)

	s.startCheckinLateRetry(context.Background(), []string{"u1"})
	time.Sleep(50 * time.Millisecond)
	if got := calls.Load(); got != 0 {
		t.Errorf("daily-checkin calls=%d want 0（已签应跳过）", got)
	}
}

// TestCheckinLateRetrySkipsDisabledAndGlobal 补签不绕过主批的跳过规则：
// 禁用账号与 global 账号都不打签到接口。
func TestCheckinLateRetrySkipsDisabledAndGlobal(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if strings.HasSuffix(r.URL.Path, "/daily-checkin") {
			calls.Add(1)
		}
		w.Write([]byte(`{"code":0,"msg":"ok","data":{}}`))
	}))
	defer srv.Close()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "dis", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	p.Add(&auth.Auth{UID: "g1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999, Domain: "www.workbuddy.ai"})
	p.Disable("dis", "test")
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{Pool: p, Upstream: up})
	setLateRetryDelays(t, time.Millisecond)

	s.startCheckinLateRetry(context.Background(), []string{"dis", "g1"})
	time.Sleep(50 * time.Millisecond)
	if got := calls.Load(); got != 0 {
		t.Errorf("daily-checkin calls=%d want 0（禁用/global 不该补签）", got)
	}
}

// TestCheckinLateRetryStopsOnCancel ctx 取消即退：不拖住优雅停机。
func TestCheckinLateRetryStopsOnCancel(t *testing.T) {
	var calls atomic.Int32
	s, p := newLateRetryS(t, 1000, &calls) // 永远失败
	setLateRetryDelays(t, time.Hour)       // 第一轮就要等一小时

	ctx, cancel := context.WithCancel(context.Background())
	s.startCheckinLateRetry(ctx, []string{"u1"})
	cancel()
	time.Sleep(30 * time.Millisecond)
	if got := calls.Load(); got != 0 {
		t.Errorf("daily-checkin calls=%d want 0（取消后不该发起补签）", got)
	}
	if p.CheckinDoneToday("u1") {
		t.Error("取消后不该有已签记账")
	}
}

// TestCheckinOneAlreadyIsIdempotent 上游「今天已签到」是幂等成功：归 already、
// err 为 nil、并且落记账（否则补签会把它当失败反复重打）。
func TestCheckinOneAlreadyIsIdempotent(t *testing.T) {
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		w.Write([]byte(`{"code":14001,"msg":"今天已签到"}`))
	}))
	defer srv.Close()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "u1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{Pool: p, Upstream: up})

	st, err := s.checkinOne(p.AuthByUID("u1"), "u1")
	if err != nil {
		t.Fatalf("already 不该返回错误: %v", err)
	}
	if st != CheckinAlready {
		t.Errorf("status=%q want already", st)
	}
	if !p.CheckinDoneToday("u1") {
		t.Error("幂等拒绝也要落记账（否则补签会反复重打）")
	}
}
