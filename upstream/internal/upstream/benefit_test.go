package upstream

import (
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"workbuddy2api/internal/auth"
)

// TestBenefitStatusPending 三态：active && !claimed && credit>0 才是「待领」。
func TestBenefitStatusPending(t *testing.T) {
	cases := []struct {
		name string
		st   BenefitStatus
		want bool
	}{
		{"可领", BenefitStatus{Active: true, Claimed: false, CreditNum: 100}, true},
		{"无活动", BenefitStatus{Active: false, Claimed: false, CreditNum: 100}, false},
		{"已领", BenefitStatus{Active: true, Claimed: true, CreditNum: 100}, false},
		{"无额度", BenefitStatus{Active: true, Claimed: false, CreditNum: 0}, false},
		{"负额度", BenefitStatus{Active: true, Claimed: false, CreditNum: -1}, false},
		{"全零", BenefitStatus{}, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := tc.st.Pending(); got != tc.want {
				t.Errorf("Pending()=%v want %v", got, tc.want)
			}
		})
	}
}

// TestGetGiftStatusParses 解析 status 响应（路径无 /v2 前缀、POST、billing 域）。
func TestGetGiftStatusParses(t *testing.T) {
	var paths, methods atomic.Value
	paths.Store("")
	methods.Store("")
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		paths.Store(r.URL.Path)
		methods.Store(r.Method)
		w.Write([]byte(`{"code":0,"msg":"OK","data":{"claimed":true,"claimed_at":"2026-09-24 18:15:15",` +
			`"active":true,"credit_num":1500,"validity_period":1,` +
			`"start_time":"2026-04-01 00:00:00","end_time":"2026-10-31 23:59:59"}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	st, err := c.GetGiftStatus(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("GetGiftStatus: %v", err)
	}
	if got := paths.Load().(string); got != "/billing/meter/check-gift-claimed" {
		t.Errorf("path=%q want /billing/meter/check-gift-claimed（带 /v2 是 404）", got)
	}
	if got := methods.Load().(string); got != http.MethodPost {
		t.Errorf("method=%q want POST", got)
	}
	if !st.Active || !st.Claimed || st.CreditNum != 1500 || st.ValidityPeriod != 1 ||
		st.EndTime != "2026-10-31 23:59:59" {
		t.Errorf("status=%+v 解析不符", st)
	}
	if st.Pending() {
		t.Error("claimed=true 时 Pending() 应为 false")
	}
}

// TestGetGiftStatusNoActivityIsNotError active=false 是正常 200（无活动），不报错。
func TestGetGiftStatusNoActivityIsNotError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"code":0,"data":{"active":false,"claimed":false,"credit_num":0}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	st, err := c.GetGiftStatus(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("无活动不应报错: %v", err)
	}
	if st.Active || st.Pending() {
		t.Errorf("status=%+v want active=false / 不待领", st)
	}
}

// TestGetGiftStatusTransportError 传输/业务失败透传（调用方据此 WARN）。
func TestGetGiftStatusTransportError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		w.Write([]byte(`boom`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.GetGiftStatus(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("500 应返回错误")
	}
}

// TestGetGiftStatusBusinessError 业务 code 非 0 → 错误。
func TestGetGiftStatusBusinessError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"code":12000,"msg":"internal"}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.GetGiftStatus(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("业务错误码应返回错误")
	}
}

// TestGetGiftStatusGlobalUsesGlobalBase global 账号走 global billing base（礼包按 realm 走 base）。
func TestGetGiftStatusGlobalUsesGlobalBase(t *testing.T) {
	var cnCalls, globalCalls atomic.Int32
	cnSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cnCalls.Add(1)
		http.Error(w, "should not be called", 500)
	}))
	defer cnSrv.Close()
	globalSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		globalCalls.Add(1)
		w.Write([]byte(`{"code":0,"data":{"active":true,"claimed":false,"credit_num":100}}`))
	}))
	defer globalSrv.Close()

	c := &Client{HTTP: globalSrv.Client(), ChatBaseCN: cnSrv.URL, BillingBaseCN: cnSrv.URL,
		BillingBaseGlobal: globalSrv.URL, GlobalEnabled: true}
	a := &auth.Auth{UID: "g1", AccessToken: "at", Domain: "www.workbuddy.ai"}
	st, err := c.GetGiftStatus(a)
	if err != nil {
		t.Fatalf("GetGiftStatus(global): %v", err)
	}
	if !st.Pending() {
		t.Errorf("status=%+v want 待领", st)
	}
	if cnCalls.Load() != 0 {
		t.Errorf("CN base 被调用 %d 次（global 应走 global base）", cnCalls.Load())
	}
	if globalCalls.Load() != 1 {
		t.Errorf("global base 调用 %d 次 want 1", globalCalls.Load())
	}
}
