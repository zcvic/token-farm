// checkin_receipt_test.go 签到回执四态：真实抓包形态 + 「code=0 但文案是失败」等边界。
package upstream

import (
	"net/http"
	"strings"
	"sync"
	"testing"

	"workbuddy2api/internal/auth"
)

// checkinResp 固定返回一个签到响应。
func checkinResp(status int, body string) *Client {
	return testClient(func(r *http.Request) (*http.Response, error) {
		return jsonResp(status, body), nil
	})
}

// 平铺成功回执（真实形态）：credit 必须从原始 body 取到，不能因为「没有信封」而丢。
func TestCheckinReceiptCheckedFlat(t *testing.T) {
	c := checkinResp(200, `{"credit":100,"streak_days":44,"is_streak_day":false}`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if err != nil {
		t.Fatalf("checked 不该带 error: %v", err)
	}
	if rec.State != CheckinChecked {
		t.Fatalf("state=%s want checked", rec.State)
	}
	if !rec.CreditKnown || rec.Credit != 100 {
		t.Errorf("credit=(%d, known=%v) want (100, true)", rec.Credit, rec.CreditKnown)
	}
	if rec.StreakDays != 44 {
		t.Errorf("streak_days=%d want 44", rec.StreakDays)
	}
	// DailyCheckin 的历史语义：成功 = nil error。
	if err := c.DailyCheckin(&auth.Auth{AccessToken: "at"}); err != nil {
		t.Errorf("DailyCheckin on success = %v want nil", err)
	}
}

// 带信封的成功回执（data.credit）：作为平铺形态的补充路径。
func TestCheckinReceiptCheckedEnvelope(t *testing.T) {
	c := checkinResp(200, `{"code":0,"msg":"ok","data":{"credit":50}}`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if err != nil || rec.State != CheckinChecked {
		t.Fatalf("state=%s err=%v", rec.State, err)
	}
	if !rec.CreditKnown || rec.Credit != 50 {
		t.Errorf("credit=(%d, known=%v) want (50, true)", rec.Credit, rec.CreditKnown)
	}
}

// 重复签到（真实形态）：HTTP 400 + code 10001。回执必须是 already，
// 且 error 语义保持不变（调度器/CLI 仍能用 IsAlreadyCheckin 判）。
func TestCheckinReceiptAlready(t *testing.T) {
	body := `{"code":10001,"msg":"今天已签到，请明天再来","requestId":"x"}`
	c := checkinResp(400, body)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if rec.State != CheckinAlready {
		t.Fatalf("state=%s want already", rec.State)
	}
	if rec.CreditKnown {
		t.Error("already 不该报积分（上游一个都没发）")
	}
	if rec.Code != 10001 {
		t.Errorf("code=%d want 10001", rec.Code)
	}
	if rec.Msg != "今天已签到，请明天再来" {
		t.Errorf("msg=%q 应当是干净的上游文案，而不是整段 error", rec.Msg)
	}
	if err == nil || !IsAlreadyCheckin(err) {
		t.Errorf("error 语义必须保持：err=%v IsAlreadyCheckin=%v", err, IsAlreadyCheckin(err))
	}
	if derr := c.DailyCheckin(&auth.Auth{AccessToken: "at"}); !IsAlreadyCheckin(derr) {
		t.Errorf("DailyCheckin 对幂等拒绝应仍可被 IsAlreadyCheckin 识别, got %v", derr)
	}
}

// code=0 但文案是失败：HTTP 上算成功、业务上是拒签。必须是 failed 且带 error，
// 否则「签到失败，已签到天数不足 7 天」会被记成签到成功、当天积分静默丢失。
func TestCheckinReceiptFailureTextWithCodeZero(t *testing.T) {
	c := checkinResp(200, `{"code":0,"msg":"签到失败，已签到天数不足 7 天"}`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if rec.State != CheckinFailed {
		t.Fatalf("state=%s want failed", rec.State)
	}
	if err == nil {
		t.Fatal("失败回执必须带 error（老调用方只看 error）")
	}
	if IsAlreadyCheckin(err) {
		t.Error("同时含「失败」与「已签到」的文案不能判成已签到")
	}
	if derr := c.DailyCheckin(&auth.Auth{AccessToken: "at"}); derr == nil {
		t.Error("DailyCheckin 对失败文案应返回 error")
	}
}

// 「请稍后重试」是 pending 而不是终态失败：调度器据此决定退避/补签。
func TestCheckinReceiptPending(t *testing.T) {
	c := checkinResp(500, `{"code":10001,"msg":"签到失败，请稍后重试"}`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if rec.State != CheckinPending {
		t.Fatalf("state=%s want pending", rec.State)
	}
	if err == nil {
		t.Fatal("pending 必须带 error（否则重试链路看不到它）")
	}
}

// 上游没带回 credit 时不得凭空造数字。
func TestCheckinReceiptCreditUnknown(t *testing.T) {
	c := checkinResp(200, `{"streak_days":3}`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if err != nil || rec.State != CheckinChecked {
		t.Fatalf("state=%s err=%v", rec.State, err)
	}
	if rec.CreditKnown {
		t.Errorf("没有 credit 字段却报了已知: %d", rec.Credit)
	}
	if rec.StreakDays != 3 {
		t.Errorf("streak_days=%d want 3", rec.StreakDays)
	}
}

// global 首选路径 404 → 回落 /v2 后成功。回执也要能走通这条候选链。
func TestCheckinReceiptGlobalFallback(t *testing.T) {
	var mu sync.Mutex
	var paths []string
	c := testClient(func(r *http.Request) (*http.Response, error) {
		mu.Lock()
		paths = append(paths, r.URL.Path)
		mu.Unlock()
		if r.URL.Path == dailyCheckinPath {
			return jsonResp(404, `{"code":404,"msg":"not found"}`), nil
		}
		return jsonResp(200, `{"credit":100}`), nil
	})
	c.GlobalEnabled = true
	c.BillingBaseGlobal = "https://billing.example"
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at", Domain: "www.workbuddy.ai"})
	if err != nil || rec.State != CheckinChecked {
		t.Fatalf("state=%s err=%v", rec.State, err)
	}
	mu.Lock()
	defer mu.Unlock()
	if len(paths) != 2 || paths[0] != dailyCheckinPath || paths[1] != dailyCheckinPathV2 {
		t.Fatalf("候选路径=%v want [%s %s]", paths, dailyCheckinPath, dailyCheckinPathV2)
	}
}

// 非 JSON 响应：parse 失败是 failed（不是 checked），且 Msg 里带得出原因。
func TestCheckinReceiptParseError(t *testing.T) {
	c := checkinResp(200, `<html>gateway</html>`)
	rec, err := c.CheckinReceipt(&auth.Auth{AccessToken: "at"})
	if rec.State != CheckinFailed {
		t.Fatalf("state=%s want failed", rec.State)
	}
	if err == nil || !strings.Contains(rec.Msg, "parse failed") {
		t.Errorf("err=%v msg=%q", err, rec.Msg)
	}
}
