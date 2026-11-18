// usage_alias.go 统一 usage 的缓存命中兼容字段（参考仓 `782a6414`，B-4）。
//
// 现象：部分上游把真实命中量放在 prompt_tokens_details.cached_tokens，同时又发
// cache_read_input_tokens: 0 / cached_tokens: 0 这类**兼容别名**。严格的下游
// （Anthropic / Responses 形状的适配层）会优先读别名，于是把「命中」读成「全量
// miss」——面板上的缓存命中率、上游成本归因全都偏低。
//
// 做法：按优先级取**第一个正值**（与参考仓一致），把它写回全部别名；一个正值都
// 没有（真 miss，或上游干脆没给）就原样返回，不臆造字段：
//
//	prompt_tokens_details.cached_tokens
//	prompt_cache_hit_tokens
//	cache_read_input_tokens
//	cached_tokens
//	input_tokens_details.cached_tokens
//
// input_tokens_details 只在**原本就有**时回填：它是 Responses 形状的嵌套字段，
// 给 Chat-only 客户端凭空造一个只会让下游误判。
package upstream

// usageCacheHitPaths 缓存命中量的取值路径，按可信度排序（先具体后通用）。
var usageCacheHitPaths = []struct {
	section string
	key     string
}{
	{"prompt_tokens_details", "cached_tokens"},
	{"", "prompt_cache_hit_tokens"},
	{"", "cache_read_input_tokens"},
	{"", "cached_tokens"},
	{"input_tokens_details", "cached_tokens"},
}

// normalizeUsageCacheAliases 把缓存命中量统一写回全部别名；无正值时原样返回。
func normalizeUsageCacheAliases(usage map[string]any) map[string]any {
	best, ok := bestUsageCacheHitTokens(usage)
	if !ok {
		return usage
	}
	out := cloneUsageMap(usage)
	out["cache_read_input_tokens"] = best
	out["cached_tokens"] = best
	out["prompt_cache_hit_tokens"] = best
	promptDetails := cloneUsageDetails(out, "prompt_tokens_details")
	promptDetails["cached_tokens"] = best
	out["prompt_tokens_details"] = promptDetails
	if _, exists := out["input_tokens_details"]; exists {
		inputDetails := cloneUsageDetails(out, "input_tokens_details")
		inputDetails["cached_tokens"] = best
		out["input_tokens_details"] = inputDetails
	}
	return out
}

// bestUsageCacheHitTokens 按优先级返回第一个 >0 的命中量。
// 0 与缺失等价（都表示「没有可用证据」），负数视为垃圾跳过。
func bestUsageCacheHitTokens(usage map[string]any) (int64, bool) {
	for _, path := range usageCacheHitPaths {
		var value any
		if path.section == "" {
			value = usage[path.key]
		} else if details, ok := usage[path.section].(map[string]any); ok {
			value = details[path.key]
		}
		if n, ok := num64(value); ok && n > 0 {
			return int64(n), true
		}
	}
	return 0, false
}

// cloneUsageMap 浅拷贝 usage（不修改上游解码出来的 map，流式逐帧路径上尤其重要）。
func cloneUsageMap(usage map[string]any) map[string]any {
	out := make(map[string]any, len(usage))
	for k, v := range usage {
		out[k] = v
	}
	return out
}

// cloneUsageDetails 浅拷贝嵌套 details；不存在或不是对象时返回空 map。
func cloneUsageDetails(usage map[string]any, key string) map[string]any {
	details, _ := usage[key].(map[string]any)
	out := make(map[string]any, len(details)+1)
	for k, v := range details {
		out[k] = v
	}
	return out
}
