// growth_bonus.go growth 域「补签卡」+ billing 域「新手礼包 / 活动补偿」接口。
// 逆向来源：下游 panel（/tmp/wb2a-panel internal/upstream/blackcat.go + scheduler/streak.go）
// 的连登管家闭环——补签保连登 + 礼包/补偿领取，同属 growth 任务体系（活跃地图家族）。
//
//		GET  /activity/growth/streak          → data.makeup_cards{balance,max}（补签卡余额，
//		                                        streak 同一响应体内，复用 streakPath 零加请求）
//		GET  /activity/growth/heatmap        → data.cells[]{date,score}（活跃地图热力格，
//	                                       score==0 判漏签）
//		POST /activity/growth/makeup-cards/use → {"target_date":"2006-01-02"}（对指定日期补签，
//	                                       保住连登连续天数；无卡/无漏签返回业务错误）
//		POST /billing/meter/claim-gift        → 新手礼包（每号一次，重复领返回业务错误）
//		POST /billing/meter/claim-compensation → 活动补偿（有则领，无则业务错误）
//
// 补签语义：昨日漏签（heatmap score==0）且有卡（balance>0）才补——连续天数一断
// 就要重攒 7 天，代价远大于一张卡；无卡/无漏签/查询失败均静默（不影响主流程）。
// 礼包/补偿语义：幂等写（每号一次/有则领），业务错误静默跳过不刷 WARN。
package upstream

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"time"

	"workbuddy2api/internal/auth"
	"workbuddy2api/internal/cst"
)

// 补签/礼包路径（panel 实测口径）。
const (
	heatmapPath           = "/activity/growth/heatmap"
	makeupCardUsePath     = "/activity/growth/makeup-cards/use"
	claimGiftPath         = "/billing/meter/claim-gift"
	claimCompensationPath = "/billing/meter/claim-compensation"
)

// GrowthMakeupCards 补签卡余额（streak 响应的 makeup_cards 段）。
type GrowthMakeupCards struct {
	Balance int `json:"balance"` // 可用补签卡数
	Max     int `json:"max"`     // 持有上限
}

// GrowthStreakWithCards 连登天数 + 补签卡余额（一次 GET /activity/growth/streak 读完；
// 与 GrowthRewardState 同响应体，只多解析 makeup_cards 段）。
type GrowthStreakWithCards struct {
	Streak struct {
		Days int `json:"days"`
	} `json:"streak"`
	MakeupCards GrowthMakeupCards `json:"makeup_cards"`
}

// GrowthStreakWithCards 读取连登天数 + 补签卡余额（GET /activity/growth/streak，
// 与 GrowthRewardState/GrowthStreak 同端点不同切片：一次请求同时供两者解析）。
func (c *Client) GrowthStreakWithCards(a *auth.Auth) (*GrowthStreakWithCards, error) {
	data, err := c.growthJSON(a, http.MethodGet, streakPath, nil)
	if err != nil {
		return nil, err
	}
	var st GrowthStreakWithCards
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, err
	}
	return &st, nil
}

// HeatmapCell 活跃地图热力格（一日一格）。
type HeatmapCell struct {
	Date  string `json:"date"`  // 2006-01-02（取前 10 位比较）
	Score int    `json:"score"` // 当日活跃计分（0 = 漏签）
}

// GrowthHeatmap 读取活跃地图热力格列表（GET /activity/growth/heatmap）。
// date 缺失时跳过该格（无漏签判据）。
func (c *Client) GrowthHeatmap(a *auth.Auth) ([]HeatmapCell, error) {
	data, err := c.growthJSON(a, http.MethodGet, heatmapPath, nil)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Cells []HeatmapCell `json:"cells"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, err
	}
	return resp.Cells, nil
}

// HeatmapDayScore 返回 cells 中 date（2006-01-02）当日的 score；
// 无该日格（活跃地图未覆盖）返回 (0, false)——调用方据 ok=false 判「无判据，不动」。
func HeatmapDayScore(cells []HeatmapCell, date string) (int, bool) {
	for _, c := range cells {
		if len(c.Date) >= 10 && c.Date[:10] == date {
			return c.Score, true
		}
	}
	return 0, false
}

// GrowthStreakFull 连登完整状态（GET /activity/growth/streak）：天数 + 补签卡 + 各档状态。
//
// 与 GrowthStreakWithCards / GrowthRewardState 同端点不同切片：一次请求同时供
// 「补签判据（卡片余额）」与「挑档（redemption_status）」使用，省掉一次往返。
type GrowthStreakFull struct {
	Streak struct {
		Days int `json:"days"`
	} `json:"streak"`
	MakeupCards      GrowthMakeupCards      `json:"makeup_cards"`
	RedemptionStatus GrowthRedemptionStatus `json:"redemption_status"`
}

// GrowthStreakFull 读取连登完整状态（GET /activity/growth/streak）。
func (c *Client) GrowthStreakFull(a *auth.Auth) (*GrowthStreakFull, error) {
	data, err := c.growthJSON(a, http.MethodGet, streakPath, nil)
	if err != nil {
		return nil, err
	}
	var st GrowthStreakFull
	if err := json.Unmarshal(data, &st); err != nil {
		return nil, err
	}
	return &st, nil
}

// HeatmapBrokenDates 返回**本月**（今天所在月的 1 号起）到昨天（不含今天）之间
// score==0 的日期，升序。
//
// 下界只能取月首，不能更宽：score==0 同时表示「那天漏签」与「那天账号还不存在」
// （探针实测：注册前的日子也是 0 分，heatmap 的 range 是近 365 天），只有上游能区分
// 两者——所以这里只做时间窗收窄，交给 UseMakeupCard 的拒绝兜底。
//
// 今天不算：当天未结束，0 分不代表漏签。today 由响应给出，不读宿主本机时钟
// （宿主若为 UTC，本地 08:00 前会算错天）。
func (c *Client) HeatmapBrokenDates(a *auth.Auth) ([]string, error) {
	data, err := c.growthJSON(a, http.MethodGet, heatmapPath, nil)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Today struct {
			Date string `json:"date"`
		} `json:"today"`
		Cells []HeatmapCell `json:"cells"`
	}
	if err := json.Unmarshal(data, &resp); err != nil {
		return nil, err
	}
	if len(resp.Today.Date) < 10 {
		// 缺 today.date 就无法界定「本月 / 今天」，宁可报错也不猜宿主时区——
		// 猜错的后果是把今天或上月最后几天当成漏签，白烧补签卡。
		return nil, fmt.Errorf("heatmap 缺 today.date（结构未知，不敢猜窗口）")
	}
	today := resp.Today.Date[:10]
	monthStart := today[:8] + "01"
	var out []string
	for _, cell := range resp.Cells {
		d := cell.Date
		if len(d) < 10 {
			continue
		}
		d = d[:10]
		if cell.Score == 0 && d >= monthStart && d < today {
			out = append(out, d)
		}
	}
	sort.Strings(out) // 上游是否保证升序未验证
	return out, nil
}

// UseMakeupCard 对指定日期使用补签卡（target_date 格式 2006-01-02，CST 自然日）。
// 无卡 / 该日无漏签 / 已补过 → 上游业务错误（*Error），调用方静默跳过。
func (c *Client) UseMakeupCard(a *auth.Auth, targetDate string) error {
	_, err := c.growthJSON(a, http.MethodPost, makeupCardUsePath,
		map[string]any{"target_date": targetDate})
	return err
}

// cstShanghai 上游自然日口径：CST（Asia/Shanghai），固定 +8，不依赖容器 tzdata。
// 统一走 internal/cst：日界散落多处定义时，改一处忘一处会静默少拿积分。
var cstShanghai = cst.Loc

// GrowthYesterdayDate 昨日的 CST 自然日（2006-01-02）。补签判据固定盯昨日：
// 连登断档只可能发生在「上一个自然日」（今日尚未结算）。
//
// 必须先 In(cstShanghai) 再 AddDate：AddDate 按**入参 Time 所在时区**做日历日减法，
// 容器时区含夏令时时，切换日的 23h/25h 会把瞬时点挪 1 小时、CST 日期错位一天
// （补签漏掉真实断档或补错日期）。先归一到 CST 再减日即与 scheduler.travelDay 同口径
// （CST 无夏令时，减一日恒为前一个 CST 自然日）。
func GrowthYesterdayDate(now time.Time) string {
	return now.In(cstShanghai).AddDate(0, 0, -1).Format("2006-01-02")
}

// ClaimGift 领取新手礼包（POST /billing/meter/claim-gift，每号一次）。
// 已领返回业务错误（*Error），调用方静默跳过。返回到账 credit。
func (c *Client) ClaimGift(a *auth.Auth) (int64, error) {
	return c.claimBillingCredit(a, claimGiftPath)
}

// ClaimCompensation 领取活动补偿（POST /billing/meter/claim-compensation，有则领）。
// 无可领返回业务错误（*Error），调用方静默跳过。返回到账 credit。
func (c *Client) ClaimCompensation(a *auth.Auth) (int64, error) {
	return c.claimBillingCredit(a, claimCompensationPath)
}

// claimBillingCredit billing 域领取类公共实现：POST path → data.credit。
// 回执字段缺失不视为失败（调用方按 0 记日志）。
func (c *Client) claimBillingCredit(a *auth.Auth, path string) (int64, error) {
	data, err := c.billingJSON(a, http.MethodPost, path, map[string]any{})
	if err != nil {
		return 0, err
	}
	var resp struct {
		Credit int64 `json:"credit"`
	}
	_ = json.Unmarshal(data, &resp)
	return resp.Credit, nil
}
