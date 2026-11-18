// timing.go 上游调用的分段耗时观测（参考仓 `75576b8b`，B-16）。
//
// 纯观测：不参与任何判定、重试或账号处置。
//
// 为什么需要：日志里的 TTFB 语义是「首个 SSE 帧 − 请求进 handler」，把选号、
// 请求体准备、建连、上游首字节、SSE 解析揉成一个数。拿它对比 curl 的
// time_starttransfer 会系统性偏高（参考仓实测 0.5s 量级），排查「这次为什么慢」
// 时分不清是本地准备 / 排队，还是上游真的慢。
//
//	Prep 本地请求体准备（脱敏 / effort 注入 / 缓存键 / 压缩 / 建请求对象）
//	Do   上游 Do 往返（含建连与首字节）；路径 fallback 逐次累加
//
// 各字段零值 = 未测量（不是 0ms）；nil *ChatTiming 的调用方零开销、零行为变化。
package upstream

import "time"

// ChatTiming 单次上游 chat 调用的分段耗时。
type ChatTiming struct {
	Prep time.Duration // 本地请求体准备
	Do   time.Duration // 上游 Do（含建连/首字节；路径 fallback 累加）
}

// Total 返回已测量的分段合计（未测量的段为零值，不计入）。
func (t ChatTiming) Total() time.Duration { return t.Prep + t.Do }
