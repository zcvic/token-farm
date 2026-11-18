// output_budget.go 输出预算下限（参考仓 `2b9b7736`）。
//
// 现象：客户端选了高档位思考，却把 max_tokens 拍得比这档思考本身要花的还小。
// 上游先花额度思考，剩下的额度只够写半句话，finish_reason 是 length——用户看到
// 的就是「用着用着断在句子中间」。同一份「写 3000 字文档」提示词在参考仓实测：
//
//	effort=high + max_tokens=2048  → total 4096  think 2048  正文 2048  length
//	effort=max  + max_tokens=2048  → total 4096  think 2048  正文 2048  length
//	effort=high + 不给额度         → total 16720 think 2998  正文 13722 stop
//
// 客户端不可能知道某个档位的思考要花多少，它只能拍一个数；这个组合错误只能由
// 网关修：显式额度低于「该档思考的下限」时抬到下限。
//
// 抬的是**上限**不是目标长度：模型写完自然停，不会因为上限变大而多写一个字，也
// 不额外计费。只改「客户端显式给了额度且额度 < 下限」这一种组合，其余一律不动：
//   - 没给 max_tokens（走上游/目录默认，本地已补 131072 之类的目录值）；
//   - 探活级额度（< 16 token，客户端只要几个字，不是长文被截）；
//   - 显式 `thinking.type=disabled`（关思考，额度与思考无关）；
//   - 档位未知（不猜，宁可不改）；
//   - 目录 maxOutputTokens 更小时以目录封顶（不能把上限抬过模型能力）。
package upstream

import "strings"

// effortOutputFloor 各档位「思考起码要花」的输出预算。
// 取值照参考仓 `output_budget.go`：low / minimal 4096、medium 6144、high 8192、
// xhigh 12288、max 16384。档位表与 effort_catalog.go 同源，只是这里按档位而不是
// 按模型索引，所以单独一份小表（改档位名时要一起改）。
var effortOutputFloor = map[string]int64{
	"minimal": 4096,
	"low":     4096,
	"medium":  6144,
	"high":    8192,
	"xhigh":   12288,
	"max":     16384,
}

// probeMaxTokens 探活级额度：只要几个 token 的请求不是「长文被截」，不动。
const probeMaxTokens = 16

// applyMinOutputBudget 按 body 里的档位与目录输出上限抬 max_tokens 下限（就地改写）。
// 调用点在 normalizeReasoningEffort 之后：档位已经降级到模型支持的那一档，
// 抬额度要按**实际生效**的档位算，而不是客户端最初要的那一档。
func applyMinOutputBudget(obj map[string]any) {
	effort := outputBudgetEffort(obj)
	if effort == "" {
		return
	}
	floor, ok := effortOutputFloor[effort]
	if !ok {
		return
	}
	model, _ := obj["model"].(string)
	catalogMax, _ := MaxOutputTokensListingV4(model, 0, nil)
	ensureMinOutputBudget(obj, floor, catalogMax)
}

// ensureMinOutputBudget 纯函数部分：目录值更小时封顶，再决定是否抬。
// 目录未知（catalogMax<=0）时按档位下限抬——这是唯一「宁可高估」的地方，
// 因为低估计的代价是正文被截断，高估的代价只是最坏情况上限变大。
func ensureMinOutputBudget(obj map[string]any, floor, catalogMax int64) {
	if catalogMax > 0 && catalogMax < floor {
		floor = catalogMax
	}
	cur, ok := outputBudgetLimit(obj)
	if !ok || cur < probeMaxTokens || cur >= floor {
		return
	}
	obj["max_tokens"] = floor
}

// outputBudgetEffort 取实际生效档位（snake 优先，camel 兜底）；关思考或档位缺失返回空串。
func outputBudgetEffort(obj map[string]any) string {
	if th, ok := obj["thinking"].(map[string]any); ok {
		if typ, _ := th["type"].(string); strings.EqualFold(strings.TrimSpace(typ), "disabled") {
			return ""
		}
	}
	for _, key := range []string{"reasoning_effort", "reasoningEffort"} {
		if s, ok := obj[key].(string); ok {
			if s = strings.ToLower(strings.TrimSpace(s)); s != "" {
				return s
			}
		}
	}
	return ""
}

// outputBudgetLimit 读显式 max_tokens；缺失 / 0 / 负数 / 小数 / 非数值一律视为
// 「没有可抬的额度」（0 与 null 的语义是「未设置」，负数是非法值，小数是垃圾）。
func outputBudgetLimit(obj map[string]any) (int64, bool) {
	switch v := obj["max_tokens"].(type) {
	case float64:
		if v <= 0 || v != float64(int64(v)) {
			return 0, false
		}
		return int64(v), true
	case int64:
		if v <= 0 {
			return 0, false
		}
		return v, true
	case int:
		if v <= 0 {
			return 0, false
		}
		return int64(v), true
	}
	return 0, false
}
