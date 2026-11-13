package server

import (
	"sync"
	"time"

	"workbuddy2api/internal/cst"
)

// degradeGate 降级状态机：passthrough 模式下请求被上游内容策略拦截时，
// 切换到 Degraded 中性提示词直到次日 00:00 CST 重置。
//
// 定位：误报处理（与 internal/upstream/sanitize.go 同哲学）——内容拦截多为
// system 来源的指纹误报，换最小中性提示词即可绕开；不是对抗框架。
//
// 状态机：进程内存、重启清零（可接受：重启极少触发，且 custom 模式根本
// 不进降级路径）。降级期内的请求直达 Degraded，不再先撞 400。
type degradeGate struct {
	mu    sync.Mutex
	until time.Time
}

// Active 当前是否处于降级期（now < until）。
func (g *degradeGate) Active() bool {
	g.mu.Lock()
	defer g.mu.Unlock()
	return time.Now().Before(g.until)
}

// Trigger 触发降级，直到次日 00:00 CST（Asia/Shanghai）。
// 已在降级期内则不续期（保持最早触发点的 00:00 重置语义）。
func (g *degradeGate) Trigger() {
	g.mu.Lock()
	defer g.mu.Unlock()
	if !time.Now().Before(g.until) {
		// 已过期/未触发 → 设新的 until；未过期则保持原 until（不续期）。
		g.until = nextMidnightCST(time.Now())
	}
}

// nextMidnightCST 返回 now 之后最近的 CST 00:00 时刻（纯函数，方便单测）。
//
// 边界语义：
//   - 23:59 → 次日 00:00（几秒后）
//   - 00:00 → 次日 00:00（刚过 00:00，下个零点是次日）
//
// 走 internal/cst 的唯一定义：宿主时区不是 UTC+8 时（NAS / 海外 VPS），
// 这里算出的「零点」会落在 CST 08:00，夜间降级窗口整体错位。
func nextMidnightCST(now time.Time) time.Time {
	midnight := cst.At(now, 0, 0, 0)
	for !midnight.After(now) {
		midnight = midnight.Add(24 * time.Hour)
	}
	return midnight
}
