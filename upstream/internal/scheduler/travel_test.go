package scheduler

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/pool"
	"workbuddy2api/internal/upstream"
)

// travelStub 模拟 growth 域全部端点，记录调用次数与请求参数。
type travelStub struct {
	buddy       string // /buddy/info 的 data.buddy 原文（"null" 或对象）
	state       string // /travel/status 的 data 原文
	firstStatus int    // /buddy/first 的 HTTP 状态码（非 0 时按业务错误返回）

	quotaAffordable int  // /buddy/quota 的 affordable
	quotaMax        int  // /buddy/quota 的 max_open_count
	quotaErr        bool // /buddy/quota 返回 500
	openErr         bool // /buddy/open 返回 500

	infoCalls, statusCalls, departCalls atomic.Int32
	claimCalls, firstCalls, agreeCalls  atomic.Int32
	quotaCalls, openCalls               atomic.Int32
	location, record, openCount         atomic.Int64
	openToken                           atomic.Value // string
}

func (s *travelStub) handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/activity/growth/buddy/info":
			s.infoCalls.Add(1)
			fmt.Fprintf(w, `{"code":0,"msg":"ok","data":{"buddy":%s}}`, s.buddy)
		case "/activity/growth/buddy/quota":
			s.quotaCalls.Add(1)
			if s.quotaErr {
				w.WriteHeader(500)
				w.Write([]byte(`{"code":500,"msg":"boom"}`))
				return
			}
			fmt.Fprintf(w, `{"code":0,"msg":"ok","data":{"affordable":%d,"max_open_count":%d}}`,
				s.quotaAffordable, s.quotaMax)
		case "/activity/growth/buddy/open":
			s.openCalls.Add(1)
			var body struct {
				Count       int    `json:"count"`
				ClientToken string `json:"client_token"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.openCount.Store(int64(body.Count))
			s.openToken.Store(body.ClientToken)
			if s.openErr {
				w.WriteHeader(500)
				w.Write([]byte(`{"code":500,"msg":"boom"}`))
				return
			}
			w.Write([]byte(`{"code":0,"msg":"ok","data":{"results":[{"instance":{"name":"橘猫"}}]}}`))
		case "/activity/growth/buddy/travel/status":
			s.statusCalls.Add(1)
			fmt.Fprintf(w, `{"code":0,"msg":"ok","data":%s}`, s.state)
		case "/activity/growth/buddy/travel/depart":
			s.departCalls.Add(1)
			var body struct {
				LocationID int `json:"location_id"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.location.Store(int64(body.LocationID))
			w.Write([]byte(`{"code":0,"msg":"ok","data":{}}`))
		case "/activity/growth/buddy/travel/claim":
			s.claimCalls.Add(1)
			var body struct {
				RecordID int64 `json:"record_id"`
			}
			_ = json.NewDecoder(r.Body).Decode(&body)
			s.record.Store(body.RecordID)
			w.Write([]byte(`{"code":0,"msg":"ok","data":{"reward_credit":9}}`))
		case "/activity/growth/buddy/first":
			s.firstCalls.Add(1)
			if s.firstStatus >= 400 {
				w.WriteHeader(s.firstStatus)
				w.Write([]byte(`{"code":400,"msg":"first_buddy task not completed yet"}`))
				return
			}
			w.Write([]byte(`{"code":0,"msg":"ok","data":{"buddy":{"id":1,"name":"档案喵"}}}`))
		case "/activity/growth/buddy/agreement":
			s.agreeCalls.Add(1)
			w.Write([]byte(`{"code":0,"msg":"ok","data":{"agreed":true}}`))
		default:
			http.Error(w, "not found", 404)
		}
	})
}

func (s *travelStub) server() *httptest.Server {
	return httptest.NewServer(s.handler())
}

// billingAndGrowthServer 同时模拟 billing（签到/余额/刷新）与 growth（旅行）端点，
// 供「签到收尾顺带跑旅行」这类跨域用例使用。
func billingAndGrowthServer(stub *travelStub) *httptest.Server {
	growth := stub.handler()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case strings.HasSuffix(r.URL.Path, "/daily-checkin"):
			w.Write([]byte(`{"code":0,"msg":"ok","data":{}}`))
		case strings.HasSuffix(r.URL.Path, "/get-user-resource"):
			w.Write([]byte(`{"code":0,"data":{"Response":{"Data":{"Accounts":[{"CycleCapacitySize":100,"CycleCapacityRemain":500,"CycleCapacityUsed":0}]}}}}`))
		case strings.HasSuffix(r.URL.Path, "/token/refresh"):
			w.Write([]byte(`{"code":0,"data":{"accessToken":"new","expiresIn":3600}}`))
		default:
			growth.ServeHTTP(w, r)
		}
	}))
}

// fastTravel 关闭账号间限速，避免测试白等 800ms。
func fastTravel(t *testing.T) {
	t.Helper()
	old := travelAccountDelay
	travelAccountDelay = 0
	t.Cleanup(func() { travelAccountDelay = old })
}

// newTravelScheduler 构造 travel 相关依赖齐全的调度器。
func newTravelScheduler(t *testing.T, srv *httptest.Server, uids ...string) (*Scheduler, *pool.Pool) {
	t.Helper()
	p := pool.New("")
	for _, uid := range uids {
		p.Add(&auth.Auth{UID: uid, AccessToken: "at", RefreshToken: "rt", ExpiresAt: 9999999999})
	}
	up := &upstream.Client{HTTP: srv.Client(), ChatBaseCN: srv.URL, BillingBaseCN: srv.URL}
	return New(Config{Pool: p, Upstream: up, CheckinHours: []int{9, 21}, KeepaliveHours: []int{22}}), p
}

// TestRunCheckinNowNoLongerTriggersTravel 签到收尾不再跑旅行（旅行已剥离为独立排程）。
// 旅行由独立时点（travel_hours）触发，与签到解耦。
func TestRunCheckinNowNoLongerTriggersTravel(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null"}
	srv := billingAndGrowthServer(stub)
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunCheckinNow()

	// 签到不再顺带跑旅行：buddy/info 不应被调用。
	if n := stub.infoCalls.Load(); n != 0 {
		t.Errorf("buddy/info calls=%d want 0（旅行已从签到剥离）", n)
	}
}

// TestRunTravelNowAdoptsOnNoBuddy 旅行独立排程：无猫 → 同意协议 + 领养。
func TestRunTravelNowAdoptsOnNoBuddy(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null"}
	srv := stub.server()
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunTravelNow()

	if n := stub.infoCalls.Load(); n != 1 {
		t.Errorf("buddy/info calls=%d want 1", n)
	}
	if n := stub.firstCalls.Load(); n != 1 {
		t.Errorf("buddy/first calls=%d want 1（无猫应尝试领养）", n)
	}
	if n := stub.agreeCalls.Load(); n != 1 {
		t.Errorf("buddy/agreement calls=%d want 1", n)
	}
}

// TestRunTravelCoversIdleAccount 旅行独立排程覆盖空闲账号并派出。
func TestRunTravelCoversIdleAccount(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: `{"id":7,"name":"档案喵"}`,
		state: `{"state":"idle","daily_limit_reached":false}`}
	srv := stub.server()
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")

	s.RunTravelNow()

	// 有猫 + idle + 未达上限 → 派出。
	if n := stub.departCalls.Load(); n != 1 {
		t.Errorf("depart calls=%d want 1", n)
	}
}

// TestRunKeepaliveDoesNotTriggerTravel 22 点保活不触发旅行：旅行独立排程。
func TestRunKeepaliveDoesNotTriggerTravel(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null"}
	srv := billingAndGrowthServer(stub)
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunKeepaliveNow()

	if n := stub.infoCalls.Load(); n != 0 {
		t.Errorf("buddy/info calls=%d want 0（保活不触发旅行）", n)
	}
	if n := stub.firstCalls.Load(); n != 0 {
		t.Errorf("buddy/first calls=%d want 0", n)
	}
}

// TestRunTravelStateMachine 表驱动覆盖状态机全部分支：每趟只做一个动作。
func TestRunTravelStateMachine(t *testing.T) {
	const buddyPresent = `{"id":7,"name":"档案喵 R"}`

	cases := []struct {
		name         string
		buddy        string
		state        string
		firstStatus  int
		wantInfo     int32
		wantStatus   int32
		wantDepart   int32
		wantClaim    int32
		wantFirst    int32
		wantAgree    int32
		wantLocation int64
		wantRecord   int64
	}{
		{
			name: "无猫-领养成功", buddy: "null",
			wantInfo: 1, wantFirst: 1, wantAgree: 1,
		},
		{
			name: "无猫-门槛未达-静默跳过", buddy: "null", firstStatus: 400,
			wantInfo: 1, wantFirst: 1, wantAgree: 1,
		},
		{
			name: "有猫-idle-未达上限-派出", buddy: buddyPresent,
			state:    `{"state":"idle","daily_limit_reached":false}`,
			wantInfo: 1, wantStatus: 1, wantDepart: 1, wantLocation: 4,
		},
		{
			name: "有猫-idle-已达上限-跳过", buddy: buddyPresent,
			state:    `{"state":"idle","daily_limit_reached":true}`,
			wantInfo: 1, wantStatus: 1,
		},
		{
			name: "有猫-traveling-跳过", buddy: buddyPresent,
			state:    `{"state":"traveling","record_id":42}`,
			wantInfo: 1, wantStatus: 1,
		},
		{
			name: "有猫-arrived-领奖", buddy: buddyPresent,
			state:    `{"state":"arrived","record_id":42,"reward_credit":9}`,
			wantInfo: 1, wantStatus: 1, wantClaim: 1, wantRecord: 42,
		},
		{
			name: "有猫-arrived-缺record_id-跳过领奖", buddy: buddyPresent,
			state:    `{"state":"arrived","record_id":0}`,
			wantInfo: 1, wantStatus: 1,
		},
		{
			name: "有猫-未知状态-跳过", buddy: buddyPresent,
			state:    `{"state":"teleporting"}`,
			wantInfo: 1, wantStatus: 1,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fastTravel(t)
			stub := &travelStub{buddy: tc.buddy, state: tc.state, firstStatus: tc.firstStatus}
			srv := stub.server()
			defer srv.Close()

			s, _ := newTravelScheduler(t, srv, "u1")
			s.RunTravelNow()

			got := map[string]int32{
				"info": stub.infoCalls.Load(), "status": stub.statusCalls.Load(),
				"depart": stub.departCalls.Load(), "claim": stub.claimCalls.Load(),
				"first": stub.firstCalls.Load(), "agreement": stub.agreeCalls.Load(),
			}
			want := map[string]int32{
				"info": tc.wantInfo, "status": tc.wantStatus,
				"depart": tc.wantDepart, "claim": tc.wantClaim,
				"first": tc.wantFirst, "agreement": tc.wantAgree,
			}
			for k, w := range want {
				if got[k] != w {
					t.Errorf("%s calls=%d want %d", k, got[k], w)
				}
			}
			if tc.wantDepart > 0 && stub.location.Load() != tc.wantLocation {
				t.Errorf("location_id=%d want %d", stub.location.Load(), tc.wantLocation)
			}
			if tc.wantClaim > 0 && stub.record.Load() != tc.wantRecord {
				t.Errorf("record_id=%d want %d", stub.record.Load(), tc.wantRecord)
			}
		})
	}
}

// runTravelStub 跑一趟旅行巡检，返回调度器；stub 的 server 随测试关闭。
func runTravelStub(t *testing.T, stub *travelStub) {
	t.Helper()
	fastTravel(t)
	srv := stub.server()
	t.Cleanup(srv.Close)
	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunTravelNow()
}

// TestRunTravelIdleOpensBlindBoxBeforeDepart 有猫 + idle：先开盲盒再派出（开盒不是
// 派出的前置，顺序反了会在开盒失败后挡住当天旅行）。
func TestRunTravelIdleOpensBlindBoxBeforeDepart(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{
		buddy:           `{"id":7,"name":"档案喵"}`,
		state:           `{"state":"idle","daily_limit_reached":false}`,
		quotaAffordable: 3, quotaMax: 5,
	}
	var mu sync.Mutex
	var order []string
	base := stub.handler()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/activity/growth/buddy/open" ||
			r.URL.Path == "/activity/growth/buddy/travel/depart" {
			mu.Lock()
			order = append(order, r.URL.Path)
			mu.Unlock()
		}
		base.ServeHTTP(w, r)
	}))
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunTravelNow()

	if n := stub.quotaCalls.Load(); n != 1 {
		t.Errorf("quota calls=%d want 1", n)
	}
	if n := stub.openCalls.Load(); n != 1 {
		t.Errorf("open calls=%d want 1", n)
	}
	if n := stub.departCalls.Load(); n != 1 {
		t.Errorf("depart calls=%d want 1（开盒不影响派出）", n)
	}
	if got := stub.openCount.Load(); got != 3 {
		t.Errorf("open count=%d want 3（min(affordable,max_open)）", got)
	}
	if tok, _ := stub.openToken.Load().(string); !strings.HasPrefix(tok, "buddy-open-") {
		t.Errorf("client_token=%q want prefix buddy-open-", tok)
	}
	if len(order) != 2 || order[0] != "/activity/growth/buddy/open" ||
		order[1] != "/activity/growth/buddy/travel/depart" {
		t.Errorf("写调用顺序=%v want [open depart]", order)
	}
}

// TestRunTravelArrivedDoesNotOpenBox arrived 先领奖，本轮不开盒也不派出。
func TestRunTravelArrivedDoesNotOpenBox(t *testing.T) {
	stub := &travelStub{
		buddy:           `{"id":7,"name":"档案喵"}`,
		state:           `{"state":"arrived","record_id":42,"reward_credit":9}`,
		quotaAffordable: 5, quotaMax: 5,
	}
	runTravelStub(t, stub)

	if n := stub.quotaCalls.Load(); n != 0 {
		t.Errorf("quota calls=%d want 0（arrived 不查盲盒配额）", n)
	}
	if n := stub.openCalls.Load(); n != 0 {
		t.Errorf("open calls=%d want 0", n)
	}
	if n := stub.claimCalls.Load(); n != 1 {
		t.Errorf("claim calls=%d want 1", n)
	}
	if n := stub.departCalls.Load(); n != 0 {
		t.Errorf("depart calls=%d want 0（领奖本轮不派出）", n)
	}
}

// TestRunTravelTravelingDoesNotOpenBox 在途/未知状态不开盒（避免打乱状态机）。
func TestRunTravelTravelingDoesNotOpenBox(t *testing.T) {
	cases := []struct{ name, state string }{
		{"traveling", `{"state":"traveling","record_id":42}`},
		{"unknown", `{"state":"teleporting"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stub := &travelStub{
				buddy: `{"id":7,"name":"档案喵"}`, state: tc.state,
				quotaAffordable: 5, quotaMax: 5,
			}
			runTravelStub(t, stub)
			if n := stub.quotaCalls.Load(); n != 0 {
				t.Errorf("quota calls=%d want 0", n)
			}
			if n := stub.openCalls.Load(); n != 0 {
				t.Errorf("open calls=%d want 0", n)
			}
			if n := stub.departCalls.Load(); n != 0 {
				t.Errorf("depart calls=%d want 0", n)
			}
		})
	}
}

// TestRunTravelZeroAffordableStillDeparts 能量不足（affordable=0）只跳过开盒，照常派出。
func TestRunTravelZeroAffordableStillDeparts(t *testing.T) {
	stub := &travelStub{
		buddy:           `{"id":7,"name":"档案喵"}`,
		state:           `{"state":"idle","daily_limit_reached":false}`,
		quotaAffordable: 0, quotaMax: 5,
	}
	runTravelStub(t, stub)

	if n := stub.openCalls.Load(); n != 0 {
		t.Errorf("open calls=%d want 0（能量不足）", n)
	}
	if n := stub.departCalls.Load(); n != 1 {
		t.Errorf("depart calls=%d want 1（盲盒不阻断派出）", n)
	}
}

// TestRunTravelQuotaErrorStillDeparts 配额查询失败只跳过开盒，照常派出。
func TestRunTravelQuotaErrorStillDeparts(t *testing.T) {
	stub := &travelStub{
		buddy:    `{"id":7,"name":"档案喵"}`,
		state:    `{"state":"idle","daily_limit_reached":false}`,
		quotaErr: true,
	}
	runTravelStub(t, stub)

	if n := stub.openCalls.Load(); n != 0 {
		t.Errorf("open calls=%d want 0（配额未知不开）", n)
	}
	if n := stub.departCalls.Load(); n != 1 {
		t.Errorf("depart calls=%d want 1（配额失败不阻断派出）", n)
	}
}

// TestRunTravelOpenErrorStillDeparts 开盒失败照常派出。
func TestRunTravelOpenErrorStillDeparts(t *testing.T) {
	stub := &travelStub{
		buddy:           `{"id":7,"name":"档案喵"}`,
		state:           `{"state":"idle","daily_limit_reached":false}`,
		quotaAffordable: 2, quotaMax: 5, openErr: true,
	}
	runTravelStub(t, stub)

	if n := stub.openCalls.Load(); n != 1 {
		t.Errorf("open calls=%d want 1（有配额就尝试）", n)
	}
	if n := stub.departCalls.Load(); n != 1 {
		t.Errorf("depart calls=%d want 1（开盒失败不阻断派出）", n)
	}
}

// TestRunTravelDailyLimitStillOpensBoxes 当日旅行名额已满不影响开盲盒（两套配额独立）。
func TestRunTravelDailyLimitStillOpensBoxes(t *testing.T) {
	stub := &travelStub{
		buddy:           `{"id":7,"name":"档案喵"}`,
		state:           `{"state":"idle","daily_limit_reached":true}`,
		quotaAffordable: 1, quotaMax: 5,
	}
	runTravelStub(t, stub)

	if n := stub.openCalls.Load(); n != 1 {
		t.Errorf("open calls=%d want 1（旅行名额满仍可开盒）", n)
	}
	if n := stub.departCalls.Load(); n != 0 {
		t.Errorf("depart calls=%d want 0（当日已派过）", n)
	}
}

// TestRunTravelBuddyOpenCountCapped 开盒次数取 min(affordable, max_open_count, 单轮上限 5)。
func TestRunTravelBuddyOpenCountCapped(t *testing.T) {
	cases := []struct {
		name                string
		affordable, maxOpen int
		want                int64
	}{
		{"单轮上限 5", 9, 9, 5},
		{"上游 max_open_count 更小", 9, 2, 2},
		{"affordable 更小", 2, 5, 2},
		{"max_open_count 缺失按不限", 4, 0, 4},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			stub := &travelStub{
				buddy:           `{"id":7,"name":"档案喵"}`,
				state:           `{"state":"idle","daily_limit_reached":false}`,
				quotaAffordable: tc.affordable, quotaMax: tc.maxOpen,
			}
			runTravelStub(t, stub)
			if got := stub.openCount.Load(); got != tc.want {
				t.Errorf("open count=%d want %d", got, tc.want)
			}
		})
	}
}

// TestRunTravelAdoptThresholdTriedOncePerDay 门槛未达（400）当日只试一次，后续巡检静默跳过。
func TestRunTravelAdoptThresholdTriedOncePerDay(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null", firstStatus: 400}
	srv := stub.server()
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.RunTravelNow()
	s.RunTravelNow()
	s.RunTravelNow()

	if n := stub.firstCalls.Load(); n != 1 {
		t.Errorf("first calls=%d want 1（当日只试一次）", n)
	}
	if n := stub.agreeCalls.Load(); n != 1 {
		t.Errorf("agreement calls=%d want 1", n)
	}
	if n := stub.infoCalls.Load(); n != 3 {
		t.Errorf("info calls=%d want 3（仍每趟查有无猫）", n)
	}
}

// TestRunTravelAdoptTriedExpiresNextDay 跨自然日后允许重新尝试领养。
func TestRunTravelAdoptTriedExpiresNextDay(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null", firstStatus: 400}
	srv := stub.server()
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "u1")
	s.markAdoptTried("u1") // 当日已试过
	s.RunTravelNow()
	if n := stub.firstCalls.Load(); n != 0 {
		t.Fatalf("first calls=%d want 0（当日已试过）", n)
	}

	s.adoptTried["u1"] = "2000-01-01" // 模拟昨日记录
	s.RunTravelNow()
	if n := stub.firstCalls.Load(); n != 1 {
		t.Errorf("first calls=%d want 1（跨日应重试）", n)
	}
}

// TestRunTravelSkipsDisabledAndFailedAccounts 禁用账号不查；单账号出错不影响后续账号。
func TestRunTravelSkipsDisabledAndFailedAccounts(t *testing.T) {
	fastTravel(t)
	var deadCalls, okDepart, disabledCalls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Header.Get("X-User-Id") == "disabled":
			disabledCalls.Add(1)
			w.WriteHeader(401)
			w.Write([]byte(`{"code":12153,"msg":"Offline user session not found"}`))
		case r.Header.Get("X-User-Id") == "dead":
			deadCalls.Add(1)
			w.WriteHeader(401)
			w.Write([]byte(`{"code":12153,"msg":"Offline user session not found"}`))
		case r.URL.Path == "/activity/growth/buddy/info":
			w.Write([]byte(`{"code":0,"data":{"buddy":{"id":1}}}`))
		case r.URL.Path == "/activity/growth/buddy/travel/status":
			w.Write([]byte(`{"code":0,"data":{"state":"idle","daily_limit_reached":false}}`))
		case r.URL.Path == "/activity/growth/buddy/travel/depart":
			okDepart.Add(1)
			w.Write([]byte(`{"code":0,"data":{}}`))
		default:
			http.Error(w, "not found", 404)
		}
	}))
	defer srv.Close()

	s, p := newTravelScheduler(t, srv, "disabled", "dead", "ok")
	p.Disable("disabled", "test")
	s.RunTravelNow()

	if n := deadCalls.Load(); n != 1 {
		t.Errorf("dead account calls=%d want 1（401 跳过本轮，不强刷 token）", n)
	}
	if n := disabledCalls.Load(); n != 0 {
		t.Errorf("disabled account calls=%d want 0", n)
	}
	// 前列账号失败不应中断遍历：末位 ok 账号照常完成派出。
	if n := okDepart.Load(); n != 1 {
		t.Errorf("ok account depart calls=%d want 1（失败账号不影响后续遍历）", n)
	}
	st, ok := p.Status("ok")
	if !ok {
		t.Fatal("ok 账号应在池中")
	}
	if st.Disabled {
		t.Errorf("ok 账号不应被影响: %+v", st)
	}
}

// TestRunTravelDisabledAccountSkipsAllCalls 禁用账号一个请求都不发。
func TestRunTravelDisabledAccountSkipsAllCalls(t *testing.T) {
	fastTravel(t)
	stub := &travelStub{buddy: "null"}
	srv := stub.server()
	defer srv.Close()

	s, p := newTravelScheduler(t, srv, "u1")
	p.Disable("u1", "test")
	s.RunTravelNow()

	if n := stub.infoCalls.Load(); n != 0 {
		t.Errorf("info calls=%d want 0（禁用账号应跳过）", n)
	}
}

// TestRunTravelActionErrorsDoNotAbort 各环节上游报错只影响本账号本轮：不 panic、不中断遍历。
func TestRunTravelActionErrorsDoNotAbort(t *testing.T) {
	fastTravel(t)
	var okDepart atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		uid := r.Header.Get("X-User-Id")
		fail := func() {
			w.WriteHeader(500)
			w.Write([]byte(`{"code":500,"msg":"boom"}`))
		}
		switch {
		case uid == "bdinfo": // 查有无猫失败
			fail()
		case uid == "status" && r.URL.Path == "/activity/growth/buddy/travel/status": // 查状态失败
			fail()
		case uid == "depart" && r.URL.Path == "/activity/growth/buddy/travel/depart": // 派出发失败
			fail()
		case uid == "claim" && r.URL.Path == "/activity/growth/buddy/travel/claim": // 领奖失败
			fail()
		case uid == "agree" && r.URL.Path == "/activity/growth/buddy/agreement": // 同意协议失败
			fail()
		case uid == "first" && r.URL.Path == "/activity/growth/buddy/first": // 领养非门槛类失败
			fail()
		case r.URL.Path == "/activity/growth/buddy/info":
			if uid == "claim" {
				w.Write([]byte(`{"code":0,"data":{"buddy":{"id":1}}}`))
				return
			}
			if uid == "depart" || uid == "status" || uid == "ok" {
				w.Write([]byte(`{"code":0,"data":{"buddy":{"id":1}}}`))
				return
			}
			w.Write([]byte(`{"code":0,"data":{"buddy":null}}`))
		case r.URL.Path == "/activity/growth/buddy/travel/status":
			if uid == "claim" {
				w.Write([]byte(`{"code":0,"data":{"state":"arrived","record_id":42}}`))
				return
			}
			w.Write([]byte(`{"code":0,"data":{"state":"idle","daily_limit_reached":false}}`))
		case r.URL.Path == "/activity/growth/buddy/travel/depart":
			okDepart.Add(1)
			w.Write([]byte(`{"code":0,"data":{}}`))
		case r.URL.Path == "/activity/growth/buddy/agreement":
			w.Write([]byte(`{"code":0,"data":{"agreed":true}}`))
		default:
			http.Error(w, "not found", 404)
		}
	}))
	defer srv.Close()

	s, _ := newTravelScheduler(t, srv, "agree", "bdinfo", "claim", "depart", "first", "ok", "status")
	s.RunTravelNow() // 不应 panic

	// 末位账号（uid 排序后 ok 排在 status 前，但都在失败账号之后）照常完成派出。
	if n := okDepart.Load(); n == 0 {
		t.Errorf("depart calls=%d want >0（个别账号失败不应中断遍历）", n)
	}
}

// TestRunTravelLoopCancelsWhenDisabled 无任何整点任务时 Run 不空转，ctx 取消即返回。
func TestRunTravelLoopCancelsWhenDisabled(t *testing.T) {
	s := &Scheduler{cfg: Config{}, adoptTried: map[string]string{}}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run 未在 ctx 取消后返回")
	}
}

// TestRunTravelLoopStopsOnCancel 有排程在等计时器时 ctx 取消应立即退出。
func TestRunTravelLoopStopsOnCancel(t *testing.T) {
	s := New(Config{CheckinHours: []int{9}, KeepaliveHours: []int{22}})
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { s.Run(ctx); close(done) }()
	time.Sleep(20 * time.Millisecond) // 让 Run 进入 select 等待
	cancel()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("Run 未在 ctx 取消后返回")
	}
}

// TestTravelDayAlignsCST 每日重置按 CST 自然日判定（UTC 17:00 已是次日 CST）。
func TestTravelDayAlignsCST(t *testing.T) {
	cases := []struct {
		name string
		in   time.Time
		want string
	}{
		{"UTC 凌晨 = CST 当日", time.Date(2026, 9, 11, 2, 0, 0, 0, time.UTC), "2026-09-11"},
		{"UTC 16:00 = CST 次日 00:00", time.Date(2026, 9, 11, 16, 0, 0, 0, time.UTC), "2026-09-12"},
		{"UTC 15:59 仍在 CST 当日", time.Date(2026, 9, 11, 15, 59, 0, 0, time.UTC), "2026-09-11"},
	}
	for _, c := range cases {
		if got := travelDay(c.in); got != c.want {
			t.Errorf("%s: travelDay=%s want %s", c.name, got, c.want)
		}
	}
}
