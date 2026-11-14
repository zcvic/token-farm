package scheduler

import (
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/pool"
	"workbuddy2api/internal/upstream"
)

// globalCheckinStub 模拟国际版签到：活动状态（active / today_checked_in 可配）+
// daily-checkin 写请求计数 + 余额查询（避免 checkinAll 的余额步骤 404 回落）。
type globalCheckinStub struct {
	active    bool
	checkedIn bool
	statusErr bool   // 状态查询 500
	checkin   string // ""=成功 / "already"=10001 已签 / "fail"=500
	daily     int64

	statusCalls  atomic.Int32
	checkinCalls atomic.Int32
}

func (s *globalCheckinStub) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/billing/meter/checkin-activity-status":
			s.statusCalls.Add(1)
			if s.statusErr {
				w.WriteHeader(500)
				w.Write([]byte(`boom`))
				return
			}
			fmt.Fprintf(w, `{"code":0,"data":{"active":%t,"today_checked_in":%t,"streak_days":3,"daily_credit":%d}}`,
				s.active, s.checkedIn, s.daily)
		case "/billing/meter/daily-checkin":
			s.checkinCalls.Add(1)
			switch s.checkin {
			case "already":
				w.WriteHeader(400)
				w.Write([]byte(`{"code":10001,"msg":"今日已签到"}`))
			case "fail":
				w.WriteHeader(500)
				w.Write([]byte(`boom`))
			default:
				w.Write([]byte(`{"code":0,"data":{}}`))
			}
		default:
			// 余额查询等只读端点：返回合法结构，避免 404 后回落放大计数。
			w.Header().Set("Content-Type", "application/json")
			w.Write([]byte(`{"Response":{"Data":{"Accounts":[{"CapacityRemain":100}]}}}`))
		}
	})
}

func globalScheduler(t *testing.T, srv *httptest.Server) (*Scheduler, *pool.Pool) {
	t.Helper()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "g1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999,
		Domain: "www.workbuddy.ai"})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL,
		BillingBaseGlobal: srv.URL, GlobalEnabled: true}
	return New(Config{Pool: p, Upstream: up}), p
}

// TestCheckinGlobalActivityOffNoWrite 活动未开启（常态）→ 只查状态，不发签到写请求。
func TestCheckinGlobalActivityOffNoWrite(t *testing.T) {
	stub := &globalCheckinStub{active: false, daily: 100}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.statusCalls.Load(); n != 1 {
		t.Errorf("status calls=%d want 1", n)
	}
	if n := stub.checkinCalls.Load(); n != 0 {
		t.Errorf("checkin calls=%d want 0（活动未开不盲签）", n)
	}
}

// TestCheckinGlobalStatusErrorNoWrite 状态查询失败 → fail-closed，不发写请求。
func TestCheckinGlobalStatusErrorNoWrite(t *testing.T) {
	stub := &globalCheckinStub{statusErr: true}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.checkinCalls.Load(); n != 0 {
		t.Errorf("checkin calls=%d want 0（查询失败不猜）", n)
	}
}

// TestCheckinGlobalAlreadyCheckedIn 上游说今天已签 → 不写，但记本机 checkin_done。
func TestCheckinGlobalAlreadyCheckedIn(t *testing.T) {
	stub := &globalCheckinStub{active: true, checkedIn: true, daily: 100}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.checkinCalls.Load(); n != 0 {
		t.Errorf("checkin calls=%d want 0（上游已是权威事实）", n)
	}
	if !p.CheckinDoneToday("g1") {
		t.Errorf("本机 checkin_done 未记（上游说已签）")
	}
}

// TestCheckinGlobalClaimsWhenActiveAndNotCheckedIn active 且未签 → 发一次写请求。
func TestCheckinGlobalClaimsWhenActiveAndNotCheckedIn(t *testing.T) {
	stub := &globalCheckinStub{active: true, checkedIn: false, daily: 100}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.checkinCalls.Load(); n != 1 {
		t.Errorf("checkin calls=%d want 1", n)
	}
	if !p.CheckinDoneToday("g1") {
		t.Errorf("签到成功应记 checkin_done")
	}
}

// TestCheckinGlobalAlreadyCheckinIdempotent 上游回 10001「今天已签到」→ 视为成功。
func TestCheckinGlobalAlreadyCheckinIdempotent(t *testing.T) {
	stub := &globalCheckinStub{active: true, checkin: "already", daily: 100}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.checkinCalls.Load(); n != 1 {
		t.Errorf("checkin calls=%d want 1", n)
	}
	if !p.CheckinDoneToday("g1") {
		t.Errorf("幂等「已签到」也应记 checkin_done")
	}
}

// TestCheckinGlobalFailureNotMarked 真失败 → 不记 checkin_done。
func TestCheckinGlobalFailureNotMarked(t *testing.T) {
	stub := &globalCheckinStub{active: true, checkin: "fail", daily: 100}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()
	s, p := globalScheduler(t, srv)

	s.checkinGlobal(p.AuthByUID("g1"))

	if p.CheckinDoneToday("g1") {
		t.Errorf("签到失败不应记 checkin_done")
	}
}

// TestCheckinGlobalEnterpriseSkipped 企业版无签到体系：零调用。
func TestCheckinGlobalEnterpriseSkipped(t *testing.T) {
	stub := &globalCheckinStub{active: true}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	p := pool.New("")
	p.Add(&auth.Auth{UID: "e1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999,
		Domain: "www.workbuddy.ai", EnterpriseID: "ent1"})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL,
		BillingBaseGlobal: srv.URL, GlobalEnabled: true}
	s := New(Config{Pool: p, Upstream: up})

	s.checkinGlobal(p.AuthByUID("e1"))

	if n := stub.statusCalls.Load(); n != 0 {
		t.Errorf("enterprise status calls=%d want 0", n)
	}
}

// TestCheckinGlobalNoTokenSkipped 无 access token → 零调用（上游必然 401）。
func TestCheckinGlobalNoTokenSkipped(t *testing.T) {
	stub := &globalCheckinStub{active: true}
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	p := pool.New("")
	p.Add(&auth.Auth{UID: "g1", RefreshToken: "rt", ExpiresAt: 9999999999,
		Domain: "www.workbuddy.ai"})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL,
		BillingBaseGlobal: srv.URL, GlobalEnabled: true}
	s := New(Config{Pool: p, Upstream: up})

	s.checkinGlobal(p.AuthByUID("g1"))

	if n := stub.statusCalls.Load(); n != 0 {
		t.Errorf("no-token status calls=%d want 0", n)
	}
}
