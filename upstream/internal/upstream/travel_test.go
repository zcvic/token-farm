package upstream

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"workbuddy2api/internal/auth"
)

// travelPath 断言请求打到 growth 域的正确路径（BASE 走 chatBase，无 /v2 前缀）。
func travelPath(r *http.Request, want string) error {
	if r.URL.Path != want {
		return errors.New("wrong path: " + r.URL.Path)
	}
	if r.Header.Get("Authorization") != "Bearer at" {
		return errors.New("missing Authorization")
	}
	if r.Header.Get("X-User-Id") != "u1" {
		return errors.New("missing X-User-Id")
	}
	return nil
}

func TestTravelStatusParsesFields(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet {
			return nil, errors.New("want GET")
		}
		if err := travelPath(r, "/activity/growth/buddy/travel/status"); err != nil {
			return nil, err
		}
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"state":"arrived","daily_limit_reached":true,"record_id":42,"reward_credit":7}}`), nil
	})
	st, err := c.TravelStatus(&auth.Auth{AccessToken: "at", UID: "u1"})
	if err != nil {
		t.Fatalf("travel status: %v", err)
	}
	if st.State != "arrived" || !st.DailyLimitReached || st.RecordID != 42 || st.RewardCredit != 7 {
		t.Errorf("state=%+v", st)
	}
}

// TestTravelStatusBusinessError 上游 400 且 body 仍是 {code,msg,data} 信封时，
// 应被 doJSON 归一为 *Error（带 HTTP 状态码），而不是解析失败。
func TestTravelStatusBusinessError(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(400, `{"code":400,"msg":"no active buddy","data":null}`), nil
	})
	_, err := c.TravelStatus(&auth.Auth{AccessToken: "at", UID: "u1"})
	var ue *Error
	if !errors.As(err, &ue) {
		t.Fatalf("want *Error, got %T %v", err, err)
	}
	if ue.Status != 400 || ue.Kind != ErrClient {
		t.Errorf("status=%d kind=%v", ue.Status, ue.Kind)
	}
}

func TestTravelDepartSendsLocationID(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, errors.New("want POST")
		}
		if err := travelPath(r, "/activity/growth/buddy/travel/depart"); err != nil {
			return nil, err
		}
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"msg":"ok","data":{}}`), nil
	})
	if err := c.TravelDepart(&auth.Auth{AccessToken: "at", UID: "u1"}, 4); err != nil {
		t.Fatalf("depart: %v", err)
	}
	var body map[string]any
	if err := json.Unmarshal(got, &body); err != nil {
		t.Fatalf("depart body: %v (%s)", err, got)
	}
	if n, _ := body["location_id"].(float64); int(n) != 4 {
		t.Errorf("location_id=%v want 4 (body=%s)", body["location_id"], got)
	}
}

func TestTravelClaimReturnsReward(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, errors.New("want POST")
		}
		if err := travelPath(r, "/activity/growth/buddy/travel/claim"); err != nil {
			return nil, err
		}
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"reward_credit":9}}`), nil
	})
	reward, err := c.TravelClaim(&auth.Auth{AccessToken: "at", UID: "u1"}, 42)
	if err != nil {
		t.Fatalf("claim: %v", err)
	}
	if reward != 9 {
		t.Errorf("reward=%d want 9", reward)
	}
	if !bytes.Contains(got, []byte(`"record_id":42`)) {
		t.Errorf("claim body missing record_id: %s", got)
	}
}

// TestBuddyInfoNullMeansNoBuddy data.buddy 为 null → 返回 nil 表示无猫。
func TestBuddyInfoNullMeansNoBuddy(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet {
			return nil, errors.New("want GET")
		}
		if err := travelPath(r, "/activity/growth/buddy/info"); err != nil {
			return nil, err
		}
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"buddy":null}}`), nil
	})
	b, err := c.BuddyInfo(&auth.Auth{AccessToken: "at", UID: "u1"})
	if err != nil {
		t.Fatalf("buddy info: %v", err)
	}
	if b != nil {
		t.Errorf("buddy=%+v want nil (无猫)", b)
	}
}

func TestBuddyInfoPresent(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(200, `{"code":0,"data":{"buddy":{"id":7,"name":"档案喵 R"}}}`), nil
	})
	b, err := c.BuddyInfo(&auth.Auth{AccessToken: "at", UID: "u1"})
	if err != nil || b == nil {
		t.Fatalf("buddy=%+v err=%v", b, err)
	}
	if b.Name != "档案喵 R" || b.ID != 7 {
		t.Errorf("buddy=%+v", b)
	}
}

func TestBuddyAgreementIdempotent(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, errors.New("want POST")
		}
		if err := travelPath(r, "/activity/growth/buddy/agreement"); err != nil {
			return nil, err
		}
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"agreed":true}}`), nil
	})
	if err := c.BuddyAgreement(&auth.Auth{AccessToken: "at", UID: "u1"}); err != nil {
		t.Fatalf("agreement: %v", err)
	}
	if !bytes.Contains(got, []byte(`"agree":true`)) {
		t.Errorf("agreement body=%s", got)
	}
}

func TestBuddyFirstPath(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, errors.New("want POST")
		}
		if err := travelPath(r, "/activity/growth/buddy/first"); err != nil {
			return nil, err
		}
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"buddy":{"id":1}}}`), nil
	})
	if err := c.BuddyFirst(&auth.Auth{AccessToken: "at", UID: "u1"}); err != nil {
		t.Fatalf("first: %v", err)
	}
	if string(bytes.TrimSpace(got)) != "{}" {
		t.Errorf("first body=%q want {}", got)
	}
}

// TestIsBuddyTaskIncomplete conversation 门槛未达标：HTTP 400 + first_buddy 关键词。
func TestIsBuddyTaskIncomplete(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(400, `{"code":400,"msg":"first_buddy task not completed yet"}`), nil
	})
	err := c.BuddyFirst(&auth.Auth{AccessToken: "at", UID: "u1"})
	if err == nil {
		t.Fatal("want error")
	}
	if !IsBuddyTaskIncomplete(err) {
		t.Errorf("err=%v should be classified as 门槛未达标", err)
	}
}

func TestIsBuddyTaskIncompleteNegative(t *testing.T) {
	cases := []struct {
		name string
		err  error
		want bool
	}{
		{"nil", nil, false},
		{"普通错误", errors.New("boom"), false},
		{"400 其他业务错误", &Error{Kind: ErrClient, Status: 400, Msg: "no active buddy"}, false},
		{"500 含关键词也不认", &Error{Kind: ErrServer, Status: 500, Msg: "first_buddy task not completed yet"}, false},
		{"401 会话失效", &Error{Kind: ErrSessionDead, Status: 401, Msg: "Offline user session not found"}, false},
	}
	for _, c := range cases {
		if got := IsBuddyTaskIncomplete(c.err); got != c.want {
			t.Errorf("%s: got %v want %v", c.name, got, c.want)
		}
	}
}

// TestBuddyQuotaParsesFields 盲盒配额：affordable / max_open_count。
func TestBuddyQuotaParsesFields(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodGet {
			return nil, errors.New("want GET")
		}
		if err := travelPath(r, "/activity/growth/buddy/quota"); err != nil {
			return nil, err
		}
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"affordable":3,"max_open_count":5,"balance":42}}`), nil
	})
	q, err := c.BuddyQuota(&auth.Auth{AccessToken: "at", UID: "u1"})
	if err != nil {
		t.Fatalf("buddy quota: %v", err)
	}
	if q.Affordable != 3 || q.MaxOpenCount != 5 {
		t.Errorf("quota=%+v want affordable=3 max_open_count=5", q)
	}
}

// TestBuddyOpenSendsCountAndToken 开盒请求带 count 与每次新建的 client_token，
// 并解析 results[].instance.name（缺失回落 template.name）。
func TestBuddyOpenSendsCountAndToken(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		if r.Method != http.MethodPost {
			return nil, errors.New("want POST")
		}
		if err := travelPath(r, "/activity/growth/buddy/open"); err != nil {
			return nil, err
		}
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"msg":"ok","data":{"results":[`+
			`{"instance":{"name":"橘猫","rarity":"R"}},`+
			`{"template":{"name":"布偶"}}]}}`), nil
	})
	names, err := c.BuddyOpen(&auth.Auth{AccessToken: "at", UID: "u1"}, 3)
	if err != nil {
		t.Fatalf("buddy open: %v", err)
	}
	var body struct {
		Count       int    `json:"count"`
		ClientToken string `json:"client_token"`
	}
	if err := json.Unmarshal(got, &body); err != nil {
		t.Fatalf("open body: %v (%s)", err, got)
	}
	if body.Count != 3 {
		t.Errorf("count=%d want 3 (body=%s)", body.Count, got)
	}
	if !strings.HasPrefix(body.ClientToken, "buddy-open-") {
		t.Errorf("client_token=%q want prefix buddy-open-", body.ClientToken)
	}
	if len(names) != 2 || names[0] != "橘猫" || names[1] != "布偶" {
		t.Errorf("names=%v want [橘猫 布偶]", names)
	}
}

// TestBuddyOpenDefaultsToOne 非法 count 兜底为 1（上游要求 count>=1）。
func TestBuddyOpenDefaultsToOne(t *testing.T) {
	var got []byte
	c := testClient(func(r *http.Request) (*http.Response, error) {
		got, _ = io.ReadAll(r.Body)
		return jsonResp(200, `{"code":0,"data":{}}`), nil
	})
	if _, err := c.BuddyOpen(&auth.Auth{AccessToken: "at", UID: "u1"}, 0); err != nil {
		t.Fatalf("buddy open: %v", err)
	}
	if !bytes.Contains(got, []byte(`"count":1`)) {
		t.Errorf("open body=%s want count=1", got)
	}
}

// TestBuddyOpenBusinessError 无猫/无活动时上游业务错误透传（调用方静默）。
func TestBuddyOpenBusinessError(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(403, `{"code":403,"msg":"no active buddy"}`), nil
	})
	if _, err := c.BuddyOpen(&auth.Auth{AccessToken: "at", UID: "u1"}, 1); err == nil {
		t.Fatal("403 应返回错误")
	}
}

// TestTravel401ClassifiedSessionDead 401 交由调用方跳过本轮（巡检不强刷 token）。
func TestTravel401ClassifiedSessionDead(t *testing.T) {
	c := testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(401, `{"code":12153,"msg":"Offline user session not found"}`), nil
	})
	_, err := c.TravelStatus(&auth.Auth{AccessToken: "at", UID: "u1"})
	var ue *Error
	if !errors.As(err, &ue) || ue.Kind != ErrSessionDead {
		t.Fatalf("err=%v want ErrSessionDead", err)
	}
}
