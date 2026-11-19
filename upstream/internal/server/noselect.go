// 选不到号时的观测日志（移植自 workbuddy2api-panel d40d4b8，issue #276）。
package server

import (
	"log"
	"sync"
	"time"

	"workbuddy2api/internal/pool"
)

// noSelectLogInterval 同一域、同一模型两条「选不到号」日志的最小间隔。
// 选号失败是请求级事件，高并发下若无门控会把日志刷爆；一分钟一条足够排障。
const noSelectLogInterval = time.Minute

var (
	noSelectMu   sync.Mutex
	noSelectLast = map[string]time.Time{}
)

// logNoSelectable 池子里挑不出可派发的账号时记一条（按域 + 模型限流，每分钟最多一条）。
// 拿不到池子（未配置）时静默返回，不影响请求路径。
//
// why 由调用方按 pick 的判据顺序算好（pool.NoPickForRealm）：这条日志的价值全在
// 「是哪一类把号挡完的」——只报 healthy>0 仍然得靠人去猜。
func (h *Handler) logNoSelectable(realm, model string, why pool.NoPick) {
	if h.cfg.Pool == nil {
		return
	}
	now := time.Now()
	key := realm + "\x1f" + model
	noSelectMu.Lock()
	if last, ok := noSelectLast[key]; ok && now.Sub(last) < noSelectLogInterval {
		noSelectMu.Unlock()
		return
	}
	noSelectLast[key] = now
	noSelectMu.Unlock()

	log.Printf("WARN: [pool] no selectable account realm=%q model=%q no_pick{%s} —— 该域本请求快速失败（503）；healthy>0 说明是全被在途/冷却挡住",
		realm, model, why.String())
}
