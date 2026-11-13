// modelhidden.go 全局隐藏名单的 Go 侧实现（models.hidden）。
//
// 为什么 Go 侧也要有：本项目的对外流量通常走 Python 网关，但上游端口是**发布到
// 宿主机**的（docker-compose 的 ports 段），直连 Go 完全可行。那种情况下 Python 的
// 隐藏名单完全失效——列表里照样出现，点名调用照样通。只藏列表不拦调用等于没藏，
// 所以这里和 Python 一样是**两道闸**：列表过滤 + 点名调用直接拒绝。
//
// 口径必须与 server/services/modelpolicy.py 完全一致，否则同一个模型在两条入口上
// 表现不同：
//   - 裸名（`glm-5.2`）= 两个域都隐藏；
//   - `cn:glm-5.2` = 只隐藏国内版；
//   - `global:glm-5.2` = 只隐藏国际版；
//   - 匹配忽略大小写，也忽略「带不带前缀」的写法差异（面板展示裸名，用户照着填）。
//
// 配置来源是 config.json 的 models.hidden——与 Python 的数据库设置之间由面板侧
// 单向镜像（见 server/services/modelpolicy.py 的 save）。Go 只读、不写。
package server

import "strings"

// hiddenRealms 认得的域前缀。与 resolveModel 的前缀枚举保持一致。
var hiddenRealms = map[string]bool{"cn": true, "global": true}

// splitRealm 把模型名拆成 (realm, 裸名)。无前缀按 cn——与 resolveModel 同口径。
func splitRealm(model string) (realm, bare string) {
	text := strings.ToLower(strings.TrimSpace(model))
	if i := strings.IndexByte(text, ':'); i >= 0 {
		if r := text[:i]; hiddenRealms[r] && i+1 < len(text) {
			return r, text[i+1:]
		}
	}
	return "cn", text
}

// hasRealmPrefix 报告条目是否显式带了域前缀。
func hasRealmPrefix(entry string) bool {
	i := strings.IndexByte(entry, ':')
	if i < 0 {
		return false
	}
	return hiddenRealms[strings.ToLower(strings.TrimSpace(entry[:i]))]
}

// modelHidden 报告 model 是否命中隐藏名单。
//
// 空模型恒为 false（/v1/models 这类不带模型的调用不该被拦）。
// 名单为空时直接返回 false——零配置对现有行为逐字节无影响。
func modelHidden(model string, hidden []string) bool {
	if strings.TrimSpace(model) == "" || len(hidden) == 0 {
		return false
	}
	realm, bare := splitRealm(model)
	exact := strings.ToLower(strings.TrimSpace(model))
	for _, entry := range hidden {
		eRealm, eBare := splitRealm(entry)
		// 条目裸名既可能对上「裸名调用」，也可能对上「带前缀调用的裸名部分」。
		if eBare != bare && eBare != exact {
			continue
		}
		// 裸名条目 = 两个域都隐藏；带前缀条目只藏对应域。
		if !hasRealmPrefix(entry) || eRealm == realm {
			return true
		}
	}
	return false
}

// filterHidden 从模型列表里剔除隐藏条目。按 id 判定，与 Python 的 filter_payload
// 同口径（它也是看 `data[].id`）。
func filterHidden(entries []map[string]any, hidden []string) []map[string]any {
	if len(hidden) == 0 || len(entries) == 0 {
		return entries
	}
	out := make([]map[string]any, 0, len(entries))
	for _, e := range entries {
		id, _ := e["id"].(string)
		if modelHidden(id, hidden) {
			continue
		}
		out = append(out, e)
	}
	return out
}
