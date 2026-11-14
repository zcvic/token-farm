package upstream

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"workbuddy2api/internal/auth"
)

// TestGetCheckinActivityStatusParses 解析 CN 口径的签到活动状态（/v2 前缀）。
func TestGetCheckinActivityStatusParses(t *testing.T) {
	var path, method atomic.Value
	path.Store("")
	method.Store("")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path.Store(r.URL.Path)
		method.Store(r.Method)
		w.Write([]byte(`{"code":0,"msg":"OK","data":{"active":true,"today_checked_in":true,"streak_days":17,` +
			`"daily_credit":100,"today_credit":100,"is_streak_day":false,"next_streak_day":0,` +
			`"streak_bonus_days":0,"streak_bonus_credit":0,"checkin_dates":["2026-10-16"],` +
			`"week_checkin_days":5,"week_progress":[true,true],"total_credits":1700,` +
			`"start_time":"2026-10-01 00:00:00","end_time":"2026-10-31 23:59:59"}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	st, err := c.GetCheckinActivityStatus(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("GetCheckinActivityStatus: %v", err)
	}
	if got := path.Load().(string); got != "/v2/billing/meter/checkin-activity-status" {
		t.Errorf("path=%q want CN 单路径 /v2/...", got)
	}
	if got := method.Load().(string); got != http.MethodPost {
		t.Errorf("method=%q want POST", got)
	}
	if !st.Active || !st.TodayCheckedIn || st.StreakDays != 17 || st.DailyCredit != 100 ||
		st.TodayCredit != 100 || st.WeekCheckinDays != 5 || st.TotalCredits != 1700 ||
		st.EndTime != "2026-10-31 23:59:59" {
		t.Errorf("status=%+v 解析不符", st)
	}
}

// TestGetCheckinActivityStatusGlobalNoPrefixFirst global 先试无 /v2 前缀（R9 口径）。
func TestGetCheckinActivityStatusGlobalNoPrefixFirst(t *testing.T) {
	var hits atomic.Int32
	var firstPath atomic.Value
	firstPath.Store("")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if hits.Add(1) == 1 {
			firstPath.Store(r.URL.Path)
		}
		w.Write([]byte(`{"code":0,"data":{"active":false}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL,
		BillingBaseGlobal: srv.URL, GlobalEnabled: true}
	a := &auth.Auth{UID: "g1", AccessToken: "at", Domain: "www.workbuddy.ai"}
	st, err := c.GetCheckinActivityStatus(a)
	if err != nil {
		t.Fatalf("GetCheckinActivityStatus(global): %v", err)
	}
	if got := firstPath.Load().(string); got != "/billing/meter/checkin-activity-status" {
		t.Errorf("首个路径=%q want 无 /v2 前缀", got)
	}
	if st.Active {
		t.Error("active 应为 false")
	}
	if n := hits.Load(); n != 1 {
		t.Errorf("请求数=%d want 1（首个路径成功不回落）", n)
	}
}

// TestGetCheckinActivityStatusGlobalFallsBackOn404 global 首路径 404 → 回落 /v2 前缀。
func TestGetCheckinActivityStatusGlobalFallsBackOn404(t *testing.T) {
	var hits atomic.Int32
	var lastPath atomic.Value
	lastPath.Store("")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		lastPath.Store(r.URL.Path)
		if r.URL.Path == "/billing/meter/checkin-activity-status" {
			http.Error(w, "not found", 404)
			return
		}
		w.Write([]byte(`{"code":0,"data":{"active":true,"today_checked_in":false,"daily_credit":100}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL,
		BillingBaseGlobal: srv.URL, GlobalEnabled: true}
	a := &auth.Auth{UID: "g1", AccessToken: "at", Domain: "www.workbuddy.ai"}
	st, err := c.GetCheckinActivityStatus(a)
	if err != nil {
		t.Fatalf("GetCheckinActivityStatus(global): %v", err)
	}
	if got := lastPath.Load().(string); got != "/v2/billing/meter/checkin-activity-status" {
		t.Errorf("最终路径=%q want /v2 前缀", got)
	}
	if n := hits.Load(); n != 2 {
		t.Errorf("请求数=%d want 2（404 后回落一次）", n)
	}
	if !st.Active || st.TodayCheckedIn || st.DailyCredit != 100 {
		t.Errorf("status=%+v 解析不符", st)
	}
}

// TestGetCheckinActivityStatusError 传输/业务失败透传（调用方 fail-closed，不发写请求）。
func TestGetCheckinActivityStatusError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		w.Write([]byte(`boom`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.GetCheckinActivityStatus(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("500 应返回错误")
	}
}

// TestGetCheckinActivityStatusEmptyBody 空 body 也算错误（不猜活动状态）。
func TestGetCheckinActivityStatusEmptyBody(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(``))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.GetCheckinActivityStatus(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("空 body 应返回错误")
	}
}
