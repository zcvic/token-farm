package upstream

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
)

// bonusStub 模拟补签卡 + 礼包/补偿端点（growth 域走 chatBase、billing 域走 billingBase）。
type bonusStub struct {
	heatErr     bool // heatmap 返回 500
	makeupErr   bool // makeup-cards/use 返回业务错误（无卡/无漏签）
	giftCredit  int64
	compCredit  int64
	heatCalls   atomic.Int32
	makeupBody  atomic.Value
	giftCalls   atomic.Int32
	compCalls   atomic.Int32
	// makeupTarget 传入的 target_date（断言用）。
}

func newBonusStub() *bonusStub {
	s := &bonusStub{giftCredit: 66, compCredit: 88}
	s.makeupBody.Store("")
	return s
}

func (s *bonusStub) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/activity/growth/heatmap":
			s.heatCalls.Add(1)
			if s.heatErr {
				w.WriteHeader(500)
				w.Write([]byte(`boom`))
				return
			}
			// 昨日一格 score=0（漏签）+ 今日一格 score=1。
			// 日期标签必须与生产侧同口径（CST 自然日，见 cstShanghai /
			// GrowthYesterdayDate）：上游 heatmap 的 date 是 CST 自然日，断言侧也用
			// CST（GrowthYesterdayDate 与 time.Now().In(cstShanghai)）。若此处用进程
			// 本地时区（容器恒 UTC），则在 UTC 16:00–24:00（= CST 次日 00:00–08:00）
			// 窗口内标签整体错一天：stub 的"今日"变成被测代码眼中的"昨日"，断言必然
			// 失败（每天固定 8 小时的时序性红灯，不是偶发抖动）。
			cstNow := time.Now().In(cstShanghai)
			yest := cstNow.AddDate(0, 0, -1).Format("2006-01-02")
			today := cstNow.Format("2006-01-02")
			fmt.Fprintf(w, `{"code":0,"data":{"cells":[{"date":"%sT00:00:00+08:00","score":0},{"date":"%s","score":1}]}}`, yest, today)
		case "/activity/growth/streak":
			// makeup_cards 段（补签卡余额 2）。
			w.Write([]byte(`{"code":0,"data":{"streak":{"days":5},"makeup_cards":{"balance":2,"max":5}}}`))
		case "/activity/growth/makeup-cards/use":
			var b struct {
				Date string `json:"target_date"`
			}
			_ = json.NewDecoder(r.Body).Decode(&b)
			s.makeupBody.Store(b.Date)
			if s.makeupErr {
				w.WriteHeader(400)
				w.Write([]byte(`{"code":400,"msg":"no makeup card available"}`))
				return
			}
			w.Write([]byte(`{"code":0,"data":{}}`))
		case "/billing/meter/claim-gift":
			s.giftCalls.Add(1)
			fmt.Fprintf(w, `{"code":0,"data":{"credit":%d}}`, s.giftCredit)
		case "/billing/meter/claim-compensation":
			s.compCalls.Add(1)
			fmt.Fprintf(w, `{"code":0,"data":{"credit":%d}}`, s.compCredit)
		default:
			http.Error(w, "not found", 404)
		}
	})
}

// TestGrowthStreakWithCards 解析 streak 响应的 makeup_cards 段（与 GrowthRewardState 同响应体）。
func TestGrowthStreakWithCards(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/activity/growth/streak" {
			http.Error(w, "not found", 404)
			return
		}
		w.Write([]byte(`{"code":0,"data":{"streak":{"days":5},"makeup_cards":{"balance":2,"max":5}}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}
	st, err := c.GrowthStreakWithCards(a)
	if err != nil {
		t.Fatalf("GrowthStreakWithCards: %v", err)
	}
	if st.Streak.Days != 5 || st.MakeupCards.Balance != 2 || st.MakeupCards.Max != 5 {
		t.Errorf("days=%d balance=%d max=%d want 5/2/5", st.Streak.Days, st.MakeupCards.Balance, st.MakeupCards.Max)
	}
}

// TestGrowthHeatmapYesterdayMissed 昨日格 score==0 判漏签；date 带 T 时段也命中（前 10 位比较）。
func TestGrowthHeatmapYesterdayMissed(t *testing.T) {
	stub := newBonusStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}
	cells, err := c.GrowthHeatmap(a)
	if err != nil {
		t.Fatalf("GrowthHeatmap: %v", err)
	}
	score, ok := HeatmapDayScore(cells, GrowthYesterdayDate(time.Now()))
	if !ok || score != 0 {
		t.Errorf("yesterday score=%d ok=%v want 0/true（漏签判据）", score, ok)
	}
	// 今日有分。
	today, ok2 := HeatmapDayScore(cells, time.Now().In(cstShanghai).Format("2006-01-02"))
	if !ok2 || today != 1 {
		t.Errorf("today score=%d ok=%v want 1/true", today, ok2)
	}
}

// TestHeatmapDayScoreMissing 无该日格返回 (0, false)。
func TestHeatmapDayScoreMissing(t *testing.T) {
	cells := []HeatmapCell{{Date: "2026-09-15", Score: 1}}
	if _, ok := HeatmapDayScore(cells, "2026-01-01"); ok {
		t.Error("无该日格应返回 ok=false")
	}
}

// TestUseMakeupCard 请求体带 target_date（补签日期）。
func TestUseMakeupCard(t *testing.T) {
	stub := newBonusStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}
	if err := c.UseMakeupCard(a, "2026-09-15"); err != nil {
		t.Fatalf("UseMakeupCard: %v", err)
	}
	if got := stub.makeupBody.Load().(string); got != "2026-09-15" {
		t.Errorf("target_date=%q want 2026-09-15", got)
	}
}

// TestUseMakeupCardBusinessError 无卡 → 业务错误透传（调用方静默跳过）。
func TestUseMakeupCardBusinessError(t *testing.T) {
	stub := newBonusStub()
	stub.makeupErr = true
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}
	if err := c.UseMakeupCard(a, "2026-09-15"); err == nil {
		t.Fatal("无卡应返回业务错误")
	}
}

// TestClaimGiftAndCompensation 礼包/补偿领取：billing 域 POST → data.credit。
func TestClaimGiftAndCompensation(t *testing.T) {
	stub := newBonusStub()
	srv := httptest.NewServer(stub.handler())
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}

	credit, err := c.ClaimGift(a)
	if err != nil || credit != 66 {
		t.Errorf("ClaimGift credit=%d err=%v want 66/nil", credit, err)
	}
	credit, err = c.ClaimCompensation(a)
	if err != nil || credit != 88 {
		t.Errorf("ClaimCompensation credit=%d err=%v want 88/nil", credit, err)
	}
	if n := stub.giftCalls.Load(); n != 1 {
		t.Errorf("gift calls=%d want 1", n)
	}
	if n := stub.compCalls.Load(); n != 1 {
		t.Errorf("comp calls=%d want 1", n)
	}
}

// TestClaimGiftBusinessError 已领（业务错误）→ 透传 err，credit=0。
func TestClaimGiftBusinessError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(400)
		w.Write([]byte(`{"code":400,"msg":"gift already claimed"}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	a := &auth.Auth{UID: "u1", AccessToken: "at"}
	credit, err := c.ClaimGift(a)
	if err == nil || credit != 0 {
		t.Errorf("已领应返回 err（credit=%d）", credit)
	}
}

// TestGrowthYesterdayDateCST 昨日日期按 CST 自然日（跨 UTC 日边界稳定）。
func TestGrowthYesterdayDateCST(t *testing.T) {
	// 2026-09-16 01:00 CST（前一日 UTC 2026-09-15 17:00）：昨日 CST = 09-15。
	now := time.Date(2026, 9, 16, 1, 0, 0, 0, cstShanghai)
	if got := GrowthYesterdayDate(now); got != "2026-09-15" {
		t.Errorf("GrowthYesterdayDate=%q want 2026-09-15", got)
	}
}

// TestGrowthYesterdayDateDSTZone 容器时区含夏令时（如 America/New_York）时，
// 昨日 CST 自然日必须仍按 CST 日界计算。
//
// 缺陷：GrowthYesterdayDate 先 AddDate(0,0,-1)（按**入参 Time 的时区**做日历日减法）
// 再转 CST，而注释声称与 scheduler.travelDay 同口径（travelDay 是「先转 CST 再取日」）。
// 在夏令时切换日，AddDate 保持墙钟时刻跨 23h/25h 的一天会把瞬时点挪 1 小时，
// CST 日期随之错位一天——补签（makeupYesterday）会漏掉真实断档或补错日期。
//
// 春令时切换日（ET 2026-03-08）：01:00 CST 落在这个 ±1h 带内。
// 修复前 buggy=2026-03-08（把「已过去的那天」算成今天）→ RED。
func TestGrowthYesterdayDateDSTZone(t *testing.T) {
	loc, err := time.LoadLocation("America/New_York")
	if err != nil {
		t.Skipf("tzdata 不可用: %v", err)
	}
	// 2026-03-08T15:30:00Z = ET 10:30（切换后）= CST 当日 23:30。
	now := time.Date(2026, 3, 8, 15, 30, 0, 0, time.UTC).In(loc)
	if got := GrowthYesterdayDate(now); got != "2026-03-07" {
		t.Errorf("GrowthYesterdayDate=%q want 2026-03-07（昨日 CST；DST 切换日不得错位）", got)
	}
}

// heatmapServer 返回固定 heatmap body 的测试 server。
func heatmapServer(t *testing.T, body string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/activity/growth/heatmap" {
			http.Error(w, "not found", 404)
			return
		}
		w.Write([]byte(body))
	}))
	t.Cleanup(srv.Close)
	return srv
}

// TestHeatmapBrokenDatesWindow 窗口收窄：只取「本月 1 号 ≤ d < today」且 score==0，
// 上月漏签 / 今天 / 未来日期 / 有分日期都不算。
func TestHeatmapBrokenDatesWindow(t *testing.T) {
	srv := heatmapServer(t, `{"code":0,"data":{"today":{"date":"2026-11-14 09:00:00"},"cells":[`+
		`{"date":"2026-10-31","score":0},`+ // 上月：不算
		`{"date":"2026-11-01","score":0},`+ // 月首漏签：算
		`{"date":"2026-11-05","score":1},`+ // 有分：不算
		`{"date":"2026-11-09","score":0},`+ // 漏签：算
		`{"date":"2026-11-14","score":0},`+ // 今天：不算（当天未结束）
		`{"date":"2026-11-20","score":0}`+ // 未来：不算
		`]}}`)

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	got, err := c.HeatmapBrokenDates(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("HeatmapBrokenDates: %v", err)
	}
	want := []string{"2026-11-01", "2026-11-09"}
	if len(got) != len(want) {
		t.Fatalf("got=%v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got=%v want %v", got, want)
		}
	}
}

// TestHeatmapBrokenDatesSorted 上游不保证顺序：输出必须升序。
func TestHeatmapBrokenDatesSorted(t *testing.T) {
	srv := heatmapServer(t, `{"code":0,"data":{"today":{"date":"2026-11-14"},"cells":[`+
		`{"date":"2026-11-09","score":0},{"date":"2026-11-02","score":0},{"date":"2026-11-05","score":0}]}}`)

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	got, err := c.HeatmapBrokenDates(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("HeatmapBrokenDates: %v", err)
	}
	want := []string{"2026-11-02", "2026-11-05", "2026-11-09"}
	if len(got) != len(want) {
		t.Fatalf("got=%v want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("got=%v want 升序 %v", got, want)
		}
	}
}

// TestHeatmapBrokenDatesMissingToday 缺 today.date → 报错不猜（猜错会白烧补签卡）。
func TestHeatmapBrokenDatesMissingToday(t *testing.T) {
	srv := heatmapServer(t, `{"code":0,"data":{"cells":[{"date":"2026-11-09","score":0}]}}`)

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.HeatmapBrokenDates(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("缺 today.date 应报错")
	}
}

// TestHeatmapBrokenDatesEmpty 全有分 → 空切片（无漏签，不写）。
func TestHeatmapBrokenDatesEmpty(t *testing.T) {
	srv := heatmapServer(t, `{"code":0,"data":{"today":{"date":"2026-11-14"},"cells":[`+
		`{"date":"2026-11-13","score":1}]}}`)

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	got, err := c.HeatmapBrokenDates(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("HeatmapBrokenDates: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("got=%v want 空（无漏签）", got)
	}
}

// TestHeatmapBrokenDatesError 上游失败透传（调用方静默跳过补签）。
func TestHeatmapBrokenDatesError(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(500)
		w.Write([]byte(`boom`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	if _, err := c.HeatmapBrokenDates(&auth.Auth{UID: "u1", AccessToken: "at"}); err == nil {
		t.Fatal("500 应返回错误")
	}
}

// TestGrowthStreakFull 一次读全量：天数 + 补签卡 + 档位状态。
func TestGrowthStreakFull(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/activity/growth/streak" {
			http.Error(w, "not found", 404)
			return
		}
		w.Write([]byte(`{"code":0,"data":{"streak":{"days":20},"makeup_cards":{"balance":3,"max":5},` +
			`"redemption_status":{"tier_7d_status":"claimed","tier_14d_status":"available",` +
			`"tier_28d_status":"locked","remaining_days":8,` +
			`"tiers":[{"tier":"7d","days":7},{"tier":"14d","days":14},{"tier":"28d","days":28}]}}}`))
	}))
	defer srv.Close()

	c := &Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	st, err := c.GrowthStreakFull(&auth.Auth{UID: "u1", AccessToken: "at"})
	if err != nil {
		t.Fatalf("GrowthStreakFull: %v", err)
	}
	if st.Streak.Days != 20 {
		t.Errorf("days=%d want 20", st.Streak.Days)
	}
	if st.MakeupCards.Balance != 3 || st.MakeupCards.Max != 5 {
		t.Errorf("cards=%d/%d want 3/5", st.MakeupCards.Balance, st.MakeupCards.Max)
	}
	if !st.RedemptionStatus.Claimed("7d") {
		t.Error("7d 应为 claimed")
	}
	if st.RedemptionStatus.Claimed("14d") {
		t.Error("14d 不应为 claimed")
	}
	if got := st.RedemptionStatus.Tier28dStatus; got != "locked" {
		t.Errorf("28d=%q want locked", got)
	}
	if len(st.RedemptionStatus.Tiers) != 3 {
		t.Errorf("tiers=%d want 3", len(st.RedemptionStatus.Tiers))
	}
	if st.RedemptionStatus.RemainingDays != 8 {
		t.Errorf("remaining_days=%d want 8", st.RedemptionStatus.RemainingDays)
	}
}
