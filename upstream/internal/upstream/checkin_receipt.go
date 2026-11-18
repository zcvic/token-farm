// checkin_receipt.go 签到回执的四态分类（参考仓 `8125831d`，M-P1-13）。
//
// 为什么需要单独一层：签到接口有三种「HTTP 上都算成功」的形态，事实却完全不同——
// 真的签上了（上游发积分）、今天早就签过（上游不发积分）、上游拒签。只把「有没有
// 报错」往上交，面板/日志只能二选一：把幂等拒绝说成「签到成功」，或者把成功说成失败。
// 同类项目在生产 1288 条签到记录里翻出 250 条幂等拒绝被报成「签到成功 (+100积分)」，
// 根因就是只看了 code 分支、没看文案。
//
// 参考仓线上实测（CN 号，同一号连打两次）：
//
//	首次  HTTP 200 {"credit":100,"streak_days":44,"is_streak_day":false}
//	重复  HTTP 400 {"code":10001,"msg":"今天已签到，请明天再来","requestId":"…"}
//
// 两个要点：
//  1. 成功回执是**平铺**的（没有 {code,msg,data} 信封），只交 env.Data 会得到 null，
//     积分数就从这里丢；
//  2. 失败回执的 HTTP 状态是 400，doJSONRaw 在 >=400 时不修信封，code/msg 只能从
//     原始 body 读，不能靠 *Error.Msg（那是截断到 200 字的整段原文）。
//
// 与判定层的关系：already 的口径**复用** isAlreadyCheckinMsg（已含「失败硬词压过
// already」的修正），这里不再另写一份正则，否则两个出口必然漂移。
package upstream

import (
	"encoding/json"
	"net/http"
	"strings"

	"workbuddy2api/internal/auth"
)

// CheckinState 一次签到调用的结果归属。
type CheckinState string

const (
	// CheckinChecked 本次真的签上了，上游已发积分。
	CheckinChecked CheckinState = "checked"
	// CheckinAlready 今天早些时候已签过，本次一个积分都没发。
	CheckinAlready CheckinState = "already"
	// CheckinPending 上游明确说「还在处理 / 稍后重试」：没签成，但可以重试。
	// 与 failed 分开是为了让日志与补签队列知道「该等一等」而不是「这个号没救了」。
	CheckinPending CheckinState = "pending"
	// CheckinFailed 上游拒签（业务错误 / 风控 / 参数）：重试通常没有不同结果。
	CheckinFailed CheckinState = "failed"
)

// checkinPendingMarkers 「上游还在处理」的文案（小写比较）。
// 这些形态在业务上都是**可重试**的：调度器的秒级退避与分钟级补签都靠它。
var checkinPendingMarkers = []string{
	"请求处理中",
	"request is being processed",
	"request processing",
	"稍后重试",
	"retry later",
	"try again later",
}

// CheckinReceipt 一次签到调用的回执。
type CheckinReceipt struct {
	State CheckinState
	// Credit 本次真实发放的积分。CreditKnown 为假时 Credit 无意义（上游没带回这个
	// 字段），调用方不得把「0」当成「发了 0 分」展示，更不得拿硬编码的 100 顶上。
	Credit      int64
	CreditKnown bool
	StreakDays  int64
	Code        int
	Msg         string
}

// CheckinReceipt 执行签到并给出四态回执。
//
// error 语义与 DailyCheckin 完全一致（幂等拒绝仍是 error，IsAlreadyCheckin 照旧可判）：
// 需要区分「本次真签到」和「今天早就签过」的调用方读回执，不需要的继续只看 error。
// 额外只补一种情形：HTTP 200 + code=0 却带失败/处理中文案时合成一个 *Error——否则
// 这种「HTTP 上成功、业务上失败」的回执会静默记成签到成功（Python 侧早已按失败处理，
// 两边口径必须一致）。
func (c *Client) CheckinReceipt(a *auth.Auth) (CheckinReceipt, error) {
	raw, data, err := c.billingMeterJSONRaw(a, c.checkinMeterPaths(a), http.MethodPost, map[string]any{})
	rec := classifyCheckin(data, raw, err)
	if err == nil && (rec.State == CheckinPending || rec.State == CheckinFailed) {
		kind := Classify(http.StatusOK, rec.Msg)
		if kind == ErrNone {
			kind = ErrClient
		}
		err = &Error{Kind: kind, Status: http.StatusOK, Msg: rec.Msg}
	}
	return rec, err
}

// classifyCheckin 把一次签到回执归到四态之一。
//
// 判定顺序（顺序本身是正确性的一部分）：pending → failed → already → checked。
//   - pending 最先：「请求处理中 / 请稍后重试」明确没签成，但属于可重试的软失败；
//   - failed 先于 already：见 checkinFailureMarkers（「签到失败，已签到天数不足 7 天」
//     同时含两个词，实际语义是失败，不能被 already 抢走）；
//   - already 先于 checked：code=0 但文案是「今天已签到」的形态（另一版本上游实测）
//     是幂等拒绝，不是本次签到成功。
func classifyCheckin(data, raw []byte, err error) CheckinReceipt {
	code, msg := checkinCodeMsg(data, raw)
	rec := CheckinReceipt{State: CheckinFailed, Code: code, Msg: msg}
	if rec.Msg == "" && err != nil {
		// 网络层 / 解析层错误的文案：与旧 IsAlreadyCheckin 同一来源（*Error.Msg）。
		rec.Msg = err.Error()
	}
	lower := strings.ToLower(rec.Msg)
	switch {
	case containsAnyMarker(lower, checkinPendingMarkers):
		rec.State = CheckinPending
	case containsAnyMarker(lower, checkinFailureMarkers):
		rec.State = CheckinFailed
	case isAlreadyCheckinMsg(rec.Msg):
		rec.State = CheckinAlready
	case err == nil:
		// 只有「没有报错、文案也不是已签到/失败」才是本次真签到。
		rec.State = CheckinChecked
		parseCheckinCredit(&rec, data, raw)
	}
	return rec
}

// parseCheckinCredit 取本次真实发放的积分。优先 data 信封，其次平铺 body
// （daily-checkin 成功回执是平铺的，见文件头注释）。
func parseCheckinCredit(rec *CheckinReceipt, data, raw []byte) {
	type creditBody struct {
		Credit     *int64 `json:"credit"`
		StreakDays int64  `json:"streak_days"`
	}
	try := func(b []byte) bool {
		if len(b) == 0 {
			return false
		}
		var v creditBody
		if json.Unmarshal(b, &v) != nil {
			return false
		}
		if v.Credit != nil {
			rec.Credit, rec.CreditKnown = *v.Credit, true
		}
		if v.StreakDays > 0 {
			rec.StreakDays = v.StreakDays
		}
		return rec.CreditKnown
	}
	if try(data) {
		return
	}
	try(raw)
}

// checkinCodeMsg 从原始 body（或信封 data）里取业务 code 与 msg 文案。
// HTTP >= 400 时上游未必带信封；成功回执大多平铺，两者都只能从 body 读。
func checkinCodeMsg(data, raw []byte) (int, string) {
	type body struct {
		Code    int    `json:"code"`
		Msg     string `json:"msg"`
		Message string `json:"message"`
	}
	read := func(b []byte) (int, string) {
		if len(b) == 0 {
			return 0, ""
		}
		var v body
		if json.Unmarshal(b, &v) != nil {
			return 0, ""
		}
		msg := strings.TrimSpace(v.Msg)
		if msg == "" {
			// 兜底形态：{code,msg,message}（部分端点用 message 而非 msg）。
			msg = strings.TrimSpace(v.Message)
		}
		return v.Code, msg
	}
	code, msg := read(raw)
	if msg == "" {
		if c2, m2 := read(data); m2 != "" || code == 0 {
			code, msg = c2, m2
		}
	}
	return code, msg
}
