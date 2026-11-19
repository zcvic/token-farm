// 选号失败的成因拆解（移植自 workbuddy2api-panel d40d4b8，issue #276）。
package pool

import (
	"fmt"
	"time"
)

// NoPick 一次「Pick 返回 nil」的成因拆解：每个账号按 pick 的实际判据顺序归一类。
//
// 为什么需要它：no_healthy_account 只有一句「没有可用账号」，但池子里明明还有
// 余额正常的号时，真实原因可能是手动停用、系统禁用、冷却/熔断、在途占满或
// 本次轮转已试过——五种成因的处置完全不同（启用 / 等冷却 / 提并发 / 换号）。
// 把这些计数同时写进 WARN 日志与 503 的 gateway_hint，排障不必再登服务器翻状态。
type NoPick struct {
	Total        int // 本域账号数（realm 过滤后）
	Healthy      int // 过了全部判据却没被选中：正常应为 0，非 0 说明 pick 里还有别的闸门
	Paused       int // 面板手动停用（manualDisabled）
	Disabled     int // 系统判定禁用
	Cooling      int // 冷却 / 熔断 / 降权 / 刷新风险（fallback 也救不回来的形态）
	InFlightFull int // 在途名额占满（pool.max_in_flight）
	Tried        int // 本次请求已试过（MaxRotate 轮转耗尽）
}

// String 紧凑单行，供日志与 503 的 gateway_hint 直接引用。
func (n NoPick) String() string {
	return fmt.Sprintf("total=%d healthy=%d cooling=%d paused=%d disabled=%d in_flight_full=%d tried=%d",
		n.Total, n.Healthy, n.Cooling, n.Paused, n.Disabled, n.InFlightFull, n.Tried)
}

// NoPickForRealm 复刻 pick 的排除顺序给账号归类；tried 为 nil 时不计轮转。
//
// 判据顺序必须与 pick 一致（tried → 禁用/停用 → 健康 → 在途占满），否则
// 「选不到」和「归到哪一类」会自相矛盾。soft 冷却在 pick 里由
// pickEarliestExpiryLocked 兜底捞回，能进兜底的号不会出现在选号失败的残留里；
// 因此这里的 Cooling 基本都是 hard 冷却 / 刷新风险这类真正不可选的形态。
func (p *Pool) NoPickForRealm(model, realm string, tried map[string]bool) NoPick {
	p.mu.RLock()
	defer p.mu.RUnlock()
	now := time.Now()
	var n NoPick
	for uid, e := range p.byUID {
		if realm != "" && e.a.Realm() != realm {
			continue
		}
		n.Total++
		healthy := e.healthy(now)
		if model != "" {
			healthy = e.healthyForModel(now, model)
		}
		switch {
		case tried != nil && tried[uid]:
			n.Tried++
		case e.disabled:
			n.Disabled++
		case e.manualDisabled:
			n.Paused++
		case !healthy:
			n.Cooling++
		case p.inFlightFull(e):
			n.InFlightFull++
		default:
			n.Healthy++
		}
	}
	return n
}
