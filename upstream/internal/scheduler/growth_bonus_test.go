package scheduler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/pool"
	"workbuddy2api/internal/upstream"
)

// bonusChainStub 模拟连登管家全链：heatmap（补签判据）+ streak（天数/补签卡/档位）
// + makeup-cards/use + check-gift-claimed + claim-gift + redeem + lottery。
//
// 与旧版的关键差别：礼包走「先查状态再领」（benefit.go），所以 stub 必须提供
// check-gift-claimed；heatmap 必须带 today.date（HeatmapBrokenDates 的窗口下界）。
type bonusChainStub struct {
	days       int      // streak.days
	daysAfter  int      // 补签成功后重读的 days（0 = 与 days 相同）
	cards      int      // 补签卡余额
	broken     []string // 漏签日期（升序，2006-01-02 形态）
	noToday    bool     // heatmap 缺 today.date
	heatErr    bool     // heatmap 500
	makeupFail int      // >0：第 N 次 use 起返回业务错误（0 = 从不失败）
	giftActive bool
	giftClaim  bool  // 上游说已领
	giftNum    int64 // 状态里的可领额度（credit_num）
	giftCredit int64 // claim 回执 credit
	giftEnd    string
	giftErr    bool // 状态查询失败

	makeupCalls  atomic.Int32
	makeupMu     sync.Mutex
	makeupDates  []string
	giftStatus   atomic.Int32
	giftCalls    atomic.Int32
	streakCalls  atomic.Int32
	redeemCalls  atomic.Int32
	chanceCalls  atomic.Int32
	drawCalls    atomic.Int32
	heatmapCalls atomic.Int32
}

func newBonusChainStub() *bonusChainStub {
	today := travelDay(time.Now())
	monthStart := today[:8] + "01"
	// 本月内、严格早于今天的三天（跨月时自动裁掉上月那几天）。
	var broken []string
	for _, d := range []string{
		travelDay(time.Now().AddDate(0, 0, -3)),
		travelDay(time.Now().AddDate(0, 0, -2)),
		travelDay(time.Now().AddDate(0, 0, -1)),
	} {
		if d >= monthStart && d < today {
			broken = append(broken, d)
		}
	}
	return &bonusChainStub{
		days: 8, cards: len(broken), broken: broken,
		giftActive: true, giftNum: 66, giftCredit: 66, giftEnd: "2026-12-31 23:59:59",
	}
}

func (s *bonusChainStub) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// 今日必须与生产侧同口径（CST 自然日）。stub 的 today.date 是
		// HeatmapBrokenDates 的唯一窗口依据，按请求现算，避免跨 CST 零点冻住。
		today := travelDay(time.Now())
		switch r.URL.Path {
		case "/activity/growth/heatmap":
			s.heatmapCalls.Add(1)
			if s.heatErr {
				w.WriteHeader(500)
				w.Write([]byte(`boom`))
				return
			}
			cells := ""
			for _, d := range s.broken {
				cells += fmt.Sprintf(`{"date":"%s","score":0},`, d)
			}
			cells += fmt.Sprintf(`{"date":"%s","score":1}`, today)
			if s.noToday {
				fmt.Fprintf(w, `{"code":0,"data":{"cells":[%s]}}`, cells)
				return
			}
			fmt.Fprintf(w, `{"code":0,"data":{"today":{"date":"%s"},"cells":[%s]}}`, today, cells)
		case "/activity/growth/streak":
			s.streakCalls.Add(1)
			days := s.days
			if s.makeupCalls.Load() > 0 && s.daysAfter > 0 {
				days = s.daysAfter
			}
			fmt.Fprintf(w, `{"code":0,"data":{"streak":{"days":%d},"makeup_cards":{"balance":%d,"max":5},`+
				`"redemption_status":{"tier_7d_status":"available","tier_14d_status":"locked","tier_28d_status":"locked",`+
				`"remaining_days":%d,"tiers":[{"tier":"7d","days":7,"credit":0,"energy":2,"cards":1,"chances":1}]}}}`,
				days, s.cards, days)
		case "/activity/growth/makeup-cards/use":
			n := s.makeupCalls.Add(1)
			var b struct {
				Date string `json:"target_date"`
			}
			_ = json.NewDecoder(r.Body).Decode(&b)
			s.makeupMu.Lock()
			s.makeupDates = append(s.makeupDates, b.Date)
			s.makeupMu.Unlock()
			if s.makeupFail > 0 && int(n) >= s.makeupFail {
				w.WriteHeader(400)
				w.Write([]byte(`{"code":400,"msg":"no makeup card available"}`))
				return
			}
			w.Write([]byte(`{"code":0,"data":{}}`))
		case "/billing/meter/check-gift-claimed":
			s.giftStatus.Add(1)
			if s.giftErr {
				w.WriteHeader(500)
				w.Write([]byte(`boom`))
				return
			}
			fmt.Fprintf(w, `{"code":0,"data":{"active":%t,"claimed":%t,"credit_num":%d,"end_time":"%s"}}`,
				s.giftActive, s.giftClaim, s.giftNum, s.giftEnd)
		case "/billing/meter/claim-gift":
			s.giftCalls.Add(1)
			fmt.Fprintf(w, `{"code":0,"data":{"credit":%d}}`, s.giftCredit)
		case "/activity/growth/redeem":
			s.redeemCalls.Add(1)
			w.Write([]byte(`{"code":0,"data":{"cards_granted":1,"credit_granted":0,"energy_granted":2,"chances_granted":1}}`))
		case "/activity/growth/lottery/chances":
			s.chanceCalls.Add(1)
			w.Write([]byte(`{"code":0,"data":{"balance":0}}`))
		case "/activity/growth/lottery/draw":
			s.drawCalls.Add(1)
			w.Write([]byte(`{"code":0,"data":{"prize_name":"p"}}`))
		default:
			http.Error(w, "not found", 404)
		}
	})
}

// bonusChainScheduler 构造带 bonus 链 stub 的调度器。
func bonusChainScheduler(t *testing.T, srv *httptest.Server) (*Scheduler, *pool.Pool) {
	t.Helper()
	p := pool.New("")
	p.Add(&auth.Auth{UID: "u1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	return New(Config{Pool: p, Upstream: up, ActivityReportCount: 1}), p
}

func bonusChainServer(t *testing.T, stub *bonusChainStub) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(stub.handler())
	t.Cleanup(srv.Close)
	return srv
}

// ---------------------------------------------------------------------------
// 补签（A-2：本月漏签全补，从最近一天往回）
// ---------------------------------------------------------------------------

// TestStreakBonusMakeupMultiDaysDescending 本月 3 天漏签 + 3 张卡 → 三天都补，
// 且从最近的一天往回（连续天数优先保最近的）。
func TestStreakBonusMakeupMultiDaysDescending(t *testing.T) {
	stub := newBonusChainStub()
	if len(stub.broken) < 2 {
		t.Skip("月初运行，本月不足两天可测（跨月窗口）")
	}
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.makeupCalls.Load(); n != int32(len(stub.broken)) {
		t.Fatalf("makeup calls=%d want %d（有卡就全补）", n, len(stub.broken))
	}
	// 倒序：最近的一天先补。
	wantFirst := stub.broken[len(stub.broken)-1]
	if got := stub.makeupDates[0]; got != wantFirst {
		t.Errorf("首次补签日期=%q want %q（从最近一天往回）", got, wantFirst)
	}
}

// TestStreakBonusMakeupRespectsPerRoundCap 每轮上限 1 → 只补 1 天，剩余留给下一轮。
func TestStreakBonusMakeupRespectsPerRoundCap(t *testing.T) {
	stub := newBonusChainStub()
	if len(stub.broken) < 2 {
		t.Skip("月初运行，本月不足两天可测（跨月窗口）")
	}
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)
	s.SetMakeupCardsPerRound(1)

	s.RunStreakBonusNow()

	if n := stub.makeupCalls.Load(); n != 1 {
		t.Errorf("makeup calls=%d want 1（schedule.makeup_cards_per_round=1）", n)
	}
}

// TestStreakBonusMakeupStopsOnReject 上游拒绝即停：注册前的日子同样是 0 分，
// 不能跳过继续（继续只会连环失败）。
func TestStreakBonusMakeupStopsOnReject(t *testing.T) {
	stub := newBonusChainStub()
	if len(stub.broken) < 3 {
		t.Skip("月初运行，本月不足三天可测（跨月窗口）")
	}
	stub.makeupFail = 2 // 第 2 次起拒绝
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.makeupCalls.Load(); n != 2 {
		t.Errorf("makeup calls=%d want 2（第二次被拒后即停）", n)
	}
}

// TestStreakBonusMakeupNoCards 无卡 → 不写；且不再重复读 streak（省一次往返）。
func TestStreakBonusMakeupNoCards(t *testing.T) {
	stub := newBonusChainStub()
	stub.cards = 0
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.makeupCalls.Load(); n != 0 {
		t.Errorf("makeup calls=%d want 0（无卡不写）", n)
	}
	if n := stub.streakCalls.Load(); n != 1 {
		t.Errorf("streak calls=%d want 1（无卡不重读，仅兑换前读一次）", n)
	}
}

// TestStreakBonusMakeupHeatmapErrorSilent heatmap 失败 → 静默跳过补签，链继续。
func TestStreakBonusMakeupHeatmapErrorSilent(t *testing.T) {
	stub := newBonusChainStub()
	stub.heatErr = true
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow() // 不 panic

	if n := stub.makeupCalls.Load(); n != 0 {
		t.Errorf("makeup calls=%d want 0（判据失败不写）", n)
	}
	if n := stub.redeemCalls.Load(); n != 1 {
		t.Errorf("redeem calls=%d want 1（补签失败不阻断兑换）", n)
	}
}

// TestStreakBonusMakeupMissingTodayDate heatmap 缺 today.date → 不猜窗口、不补签。
func TestStreakBonusMakeupMissingTodayDate(t *testing.T) {
	stub := newBonusChainStub()
	stub.noToday = true
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.makeupCalls.Load(); n != 0 {
		t.Errorf("makeup calls=%d want 0（缺 today.date 不猜）", n)
	}
}

// TestStreakBonusMakeupRestoresTierForRedeem 补签恢复天数后本日 redeem 直接吃到：
// 补签前 days=6（未达 7d），补签后 days=7 → redeem 发生。
func TestStreakBonusMakeupRestoresTierForRedeem(t *testing.T) {
	stub := newBonusChainStub()
	stub.days = 6
	stub.daysAfter = 7
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.redeemCalls.Load(); n != 1 {
		t.Errorf("redeem calls=%d want 1（补签恢复天数 → 7d 档当日可兑）", n)
	}
}

// ---------------------------------------------------------------------------
// 礼包（A-1：先查状态再领；A-8：guard 防同期重复）
// ---------------------------------------------------------------------------

// TestStreakBonusGiftPendingClaims 待领 → 领一次，且按 end_time 不重复。
func TestStreakBonusGiftPendingClaims(t *testing.T) {
	stub := newBonusChainStub()
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.giftStatus.Load(); n != 1 {
		t.Errorf("gift status calls=%d want 1（先查后领）", n)
	}
	if n := stub.giftCalls.Load(); n != 1 {
		t.Errorf("gift claim calls=%d want 1", n)
	}
	// 同一期（同 end_time）再跑一轮：状态仍说可领（上游 claimed 字段不可靠），
	// 但 guard 已记账 → 不再打 claim。
	s.RunStreakBonusNow()
	if n := stub.giftCalls.Load(); n != 1 {
		t.Errorf("gift claim calls after repeat=%d want 1（同期 guard）", n)
	}
}

// TestStreakBonusGiftNotPendingNoClaim active=false → 无活动，不领也不报错。
func TestStreakBonusGiftNotPendingNoClaim(t *testing.T) {
	stub := newBonusChainStub()
	stub.giftActive = false
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.giftCalls.Load(); n != 0 {
		t.Errorf("gift claim calls=%d want 0（无活动）", n)
	}
}

// TestStreakBonusGiftAlreadyClaimedNoClaim claimed=true → 不领。
func TestStreakBonusGiftAlreadyClaimedNoClaim(t *testing.T) {
	stub := newBonusChainStub()
	stub.giftClaim = true
	srv := bonusChainServer(t, stub)
	s, _ := bonusChainScheduler(t, srv)

	s.RunStreakBonusNow()

	if n := stub.giftCalls.Load(); n != 0 {
		t.Errorf("gift claim calls=%d want 0（已领）", n)
	}
}

// TestStreakBonusGiftZeroCreditSilent 上游对「已领过」回 200 + 无 credit 字段：
// credit<=0 不再打成功行（旧实现的假「+0c」来源），但 guard 照样记上。
func TestStreakBonusGiftZeroCreditSilent(t *testing.T) {
	stub := newBonusChainStub()
	stub.giftCredit = 0 // 状态说可领 66，但领取回执没有 credit 字段
	srv := bonusChainServer(t, stub)
	s, p := bonusChainScheduler(t, srv)

	if got := s.claimGiftQuiet(p.AuthByUID("u1")); got != 0 {
		t.Errorf("claimGiftQuiet=%d want 0", got)
	}
	// guard 已记 → 再问不再打 claim（状态端点仍会被查）。
	s.claimGiftQuiet(p.AuthByUID("u1"))
	if n := stub.giftCalls.Load(); n != 1 {
		t.Errorf("gift claim calls=%d want 1（credit<=0 也要记账防重试）", n)
	}
}

// TestStreakBonusGiftNewPeriodRetries end_time 变了 = 新一期 → guard key 不同，重新尝试。
func TestStreakBonusGiftNewPeriodRetries(t *testing.T) {
	stub := newBonusChainStub()
	srv := bonusChainServer(t, stub)
	s, p := bonusChainScheduler(t, srv)
	a := p.AuthByUID("u1")

	s.claimGiftQuiet(a)
	stub.giftEnd = "2027-01-31 23:59:59" // 新一期
	s.claimGiftQuiet(a)

	if n := stub.giftCalls.Load(); n != 2 {
		t.Errorf("gift claim calls=%d want 2（新一期应重试）", n)
	}
}

// TestStreakBonusGiftStatusErrorNoClaim 状态查询失败 → 不盲领。
func TestStreakBonusGiftStatusErrorNoClaim(t *testing.T) {
	stub := newBonusChainStub()
	stub.giftErr = true
	srv := bonusChainServer(t, stub)
	s, p := bonusChainScheduler(t, srv)

	if got := s.claimGiftQuiet(p.AuthByUID("u1")); got != 0 {
		t.Errorf("claimGiftQuiet=%d want 0", got)
	}
	if n := stub.giftCalls.Load(); n != 0 {
		t.Errorf("gift claim calls=%d want 0（查询失败不盲领）", n)
	}
}

// TestStreakBonusGiftEnterpriseSilent 企业版查不到礼包属预期，静默且不领。
func TestStreakBonusGiftEnterpriseSilent(t *testing.T) {
	stub := newBonusChainStub()
	stub.giftErr = true
	srv := bonusChainServer(t, stub)
	p := pool.New("")
	p.Add(&auth.Auth{UID: "e1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999,
		EnterpriseID: "e1"})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{Pool: p, Upstream: up})

	if got := s.claimGiftQuiet(p.AuthByUID("e1")); got != 0 {
		t.Errorf("claimGiftQuiet=%d want 0", got)
	}
	if n := stub.giftCalls.Load(); n != 0 {
		t.Errorf("gift claim calls=%d want 0", n)
	}
}

// TestStreakBonusEnterpriseSkipped 企业版整号跳过连登管家（无个人成长体系）。
func TestStreakBonusEnterpriseSkipped(t *testing.T) {
	stub := newBonusChainStub()
	srv := bonusChainServer(t, stub)
	p := pool.New("")
	p.Add(&auth.Auth{UID: "e1", AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999,
		EnterpriseID: "e1"})
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	s := New(Config{Pool: p, Upstream: up})

	s.RunStreakBonusNow()

	if n := stub.streakCalls.Load(); n != 0 {
		t.Errorf("enterprise streak calls=%d want 0", n)
	}
	if n := stub.giftStatus.Load(); n != 0 {
		t.Errorf("enterprise gift status calls=%d want 0", n)
	}
}
