// schema_normalize.go 出站前规整工具参数 schema。
//
// 上游按 Go 强类型反序列化 tools[].function.parameters，schema 形态或取值类型不符
// 就整单 400 code=11129「invalid function call parameters」（displayMsg「工具定义
// 不合规」）——同一份 body 换任何账号都是同一个拒绝，属于请求级错误，不是账号故障。
// 弱类型客户端（Python dict、PHP array、手写 JSON 模板）序列化出来很容易踩到：
// nil 键、类型别名、重复 required、字符串形态的数值/布尔关键字、非数组 enum、顶层
// $ref……这些都在这里就地纠偏；纠不动就删键（留着非法值等于保证整颗工具必然被拒）。
//
// 只作用于 tools[] / functions[] 子树的 parameters，不碰 response_format 等其它位置；
// 独立于 sanitize 开关：这是「让请求通过」的安全网，不是脱敏。
package upstream

import (
	"math"
	"strconv"
	"strings"
)

// normalizeToolParameters 归一化工具顶层参数定义（补 type: object / properties: {}），
// 并递归规整深层 Schema 树。nil 与非对象直接退化成无参 object。
func normalizeToolParameters(node any) any {
	if node == nil {
		return map[string]any{"type": "object", "properties": map[string]any{}}
	}
	params, ok := node.(map[string]any)
	if !ok {
		return map[string]any{"type": "object", "properties": map[string]any{}}
	}
	// 顶层 $ref：整份参数 schema 指向 $defs 时上游一刀拒收（实测 400 11129），而
	// 嵌套 $ref 实测 200——只有顶层要处理。能就地展开就展开（保住真实参数定义），
	// 展不开（非 #/$defs/ 的 ref、目标缺失、循环）也必须删掉：留着等于保证这颗工具
	// 每次请求都 400，删掉至少退化成一个无参工具。
	if ref, ok := params["$ref"].(string); ok {
		delete(params, "$ref")
		if defs, ok := params["$defs"].(map[string]any); ok && strings.HasPrefix(ref, "#/$defs/") {
			if t, ok := inlineKimiSchema(map[string]any{"$ref": ref}, defs, nil, 0).(map[string]any); ok {
				for k, v := range t {
					if _, dup := params[k]; !dup {
						params[k] = v
					}
				}
			}
		}
	}
	t, ok := params["type"].(string)
	if !ok || strings.TrimSpace(t) == "" {
		params["type"] = "object"
	} else {
		switch strings.ToLower(strings.TrimSpace(t)) {
		case "str":
			params["type"] = "string"
		case "int":
			params["type"] = "integer"
		case "float":
			params["type"] = "number"
		case "bool":
			params["type"] = "boolean"
		case "dict":
			params["type"] = "object"
		case "list":
			params["type"] = "array"
		}
	}
	if params["type"] == "object" {
		if _, hasProps := params["properties"].(map[string]any); !hasProps {
			params["properties"] = map[string]any{}
		}
	}
	normalizeSchemaTree(params)
	return params
}

// schemaContainerKeys 容器级 map：自身的键是属性名 / 定义名（用户数据），绝不能当成
// Schema 关键字改写；递归时只遍历其子 Schema 节点。
//
// dependentRequired 的值是属性名数组、dependencies（draft-7）的值是属性名数组或子
// schema：键都是属性名。不当容器时，属性名叫 items / type 会被当成关键字改写
// （["type"] 被换成 {"type":"string"}）。
var schemaContainerKeys = map[string]bool{
	"properties":        true,
	"patternProperties": true,
	"$defs":             true,
	"definitions":       true,
	"dependentSchemas":  true,
	"dependentRequired": true,
	"dependencies":      true,
}

// schemaDataKeys 取值是**数据**而不是 schema 的关键字：default / const / enum /
// example(s) 里的对象就是客户端声明的取值本身，里面出现 type、items、required、null
// 也只是数据。规整只作用于 schema，绝不递归进这些值——否则改写的是模型看到的默认值、
// 常量和枚举项（如 default {"type":"str"} 被改成 "string"）。
var schemaDataKeys = map[string]bool{
	"default":  true,
	"const":    true,
	"enum":     true,
	"example":  true,
	"examples": true,
}

// 三组关键字的取值类型同样被上游按 Go 强类型校验，类型不符即整体拒收（code=11129）。
// 2026-11-18 真机逐形态单发实测：
//
//	minimum:"1"        → 400 11129      minimum:1        → 200
//	uniqueItems:"true" → 400 11129      uniqueItems:true → 200
//	description:123    → 400 11129      description:"x"  → 200
//	enum:"x"           → 400 11129      enum:["x"]       → 200
//	additionalProperties:"false" → 400 11129
//
// 同一批实测里联合类型 type:["string","null"]、oneOf、嵌套 $ref + $defs、items:{}、
// required 指向不存在的属性全部 200，所以只纠这三组与顶层 $ref，不扩面。
var schemaNumberKeys = []string{
	"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
	"minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties",
}
var schemaBoolKeys = []string{"uniqueItems", "deprecated", "readOnly", "writeOnly"}
var schemaStringKeys = []string{"description", "title", "format", "pattern"}

// coerceSchemaKeywords 把三组关键字的取值纠成上游能接受的基本类型；纠不动就删键。
func coerceSchemaKeywords(n map[string]any) {
	for _, k := range schemaNumberKeys {
		if v, ok := n[k]; ok {
			if num, ok := schemaNumber(v); ok {
				n[k] = num
			} else {
				delete(n, k)
			}
		}
	}
	for _, k := range schemaBoolKeys {
		if v, ok := n[k]; ok {
			if b, ok := schemaBool(v); ok {
				n[k] = b
			} else {
				delete(n, k)
			}
		}
	}
	for _, k := range schemaStringKeys {
		if v, ok := n[k]; ok {
			if _, isStr := v.(string); !isStr {
				delete(n, k)
			}
		}
	}
	if v, ok := n["additionalProperties"]; ok {
		if _, isSchema := v.(map[string]any); !isSchema {
			if b, ok := schemaBool(v); ok {
				n["additionalProperties"] = b
			} else {
				delete(n, "additionalProperties")
			}
		}
	}
	for _, k := range []string{"enum", "examples"} {
		if v, ok := n[k]; ok {
			if _, isList := v.([]any); !isList {
				delete(n, k)
			}
		}
	}
}

// schemaNumber 纠数值关键字：数字原样，纯数字字符串转数值，其余（bool / 对象 /
// NaN / Infinity）判非法。encoding/json 解出来的数字一律 float64。
func schemaNumber(v any) (any, bool) {
	switch x := v.(type) {
	case float64:
		if math.IsNaN(x) || math.IsInf(x, 0) {
			return nil, false
		}
		return x, true
	case string:
		f, err := strconv.ParseFloat(strings.TrimSpace(x), 64)
		if err != nil || math.IsNaN(f) || math.IsInf(f, 0) {
			return nil, false
		}
		return f, true
	}
	return nil, false
}

// schemaBool 纠布尔关键字：bool 原样，"true"/"false" 转义，其余判非法。
func schemaBool(v any) (bool, bool) {
	switch x := v.(type) {
	case bool:
		return x, true
	case string:
		switch strings.ToLower(strings.TrimSpace(x)) {
		case "true":
			return true, true
		case "false":
			return false, true
		}
	}
	return false, false
}

// normalizeSchemaTree 递归归一化工具参数 schema 树：
//   - 关键字取值类型纠偏（数值/布尔/字符串关键字，见 coerceSchemaKeywords）；
//   - 剔除值为 nil 的关键字（description:null、title:null 导致上游反序列化失败），
//     数据关键字（default / const / enum / example(s)）的 null 保留；
//   - 类型别名归一化（str/int/bool/dict/list 等映射到 JSON Schema 标准）；
//   - required 规整（剔除 nil、空串、非 string 元素并去重，空列表直接删键）；
//   - array items 规整（解包 tuple 模式，清理 nil items）；
//   - pattern 值与 patternProperties 键里的 `\_` → `_`（map 键不可原地改，命中时
//     重建该层）；
//   - 只有 enum、没有 type 的属性补上可无损推断的类型（见 inferEnumType）；
//   - 数据关键字不递归、容器键只遍历子 Schema。
func normalizeSchemaTree(node any) {
	switch n := node.(type) {
	case map[string]any:
		// 0. 关键字取值类型纠偏放最前：纠不动的键被删掉，后面的 nil 剔除与类型推断
		// 就只看剩下的合法值。
		coerceSchemaKeywords(n)
		// 1. 剔除值为 nil 的关键字（避免上游 Go 强类型反序列化失败报 11129）。
		// 数据关键字除外：const:null（只能是 null）、default:null（缺省为 null）是
		// 有意义的取值，删了就改了语义。
		for k, v := range n {
			if v == nil && !schemaDataKeys[k] {
				delete(n, k)
			}
		}

		// 2. 类型别名归一化
		if t, ok := n["type"].(string); ok {
			switch strings.ToLower(strings.TrimSpace(t)) {
			case "str":
				n["type"] = "string"
			case "int":
				n["type"] = "integer"
			case "float":
				n["type"] = "number"
			case "bool":
				n["type"] = "boolean"
			case "dict":
				n["type"] = "object"
			case "list":
				n["type"] = "array"
			case "":
				if _, hasProps := n["properties"]; hasProps {
					n["type"] = "object"
				}
			}
		}

		// 3. 规整 required：重复项同样 400 11129（真机 ["a","a"]）。
		if req, hasReq := n["required"]; hasReq {
			if reqList, ok := req.([]any); ok {
				var cleaned []any
				seen := make(map[string]bool, len(reqList))
				for _, item := range reqList {
					if s, ok := item.(string); ok {
						s = strings.TrimSpace(s)
						if s != "" && !seen[s] {
							seen[s] = true
							cleaned = append(cleaned, s)
						}
					}
				}
				if len(cleaned) == 0 {
					delete(n, "required")
				} else {
					n["required"] = cleaned
				}
			} else {
				delete(n, "required")
			}
		}

		// 4. 规整 array items（tuple 模式取第一个 dict 子 schema）
		if items, hasItems := n["items"]; hasItems {
			if items == nil {
				delete(n, "items")
			} else if itemList, ok := items.([]any); ok {
				var firstDict map[string]any
				for _, it := range itemList {
					if d, ok := it.(map[string]any); ok {
						firstDict = d
						break
					}
				}
				if firstDict != nil {
					n["items"] = firstDict
				} else {
					n["items"] = map[string]any{"type": "string"}
				}
			}
		}

		// 5. pattern 转义修复
		if p, ok := n["pattern"].(string); ok && strings.Contains(p, `\_`) {
			n["pattern"] = strings.ReplaceAll(p, `\_`, `_`)
		}
		if props, ok := n["patternProperties"].(map[string]any); ok {
			rebuilt := false
			fixed := make(map[string]any, len(props))
			for k, v := range props {
				if strings.Contains(k, `\_`) {
					k = strings.ReplaceAll(k, `\_`, `_`)
					rebuilt = true
				}
				fixed[k] = v
			}
			if rebuilt {
				n["patternProperties"] = fixed
			}
		}

		// 6. enum 缺 type 自动推断
		if _, hasType := n["type"]; !hasType {
			if e, ok := n["enum"].([]any); ok && len(e) > 0 {
				if t := inferEnumType(e); t != "" {
					n["type"] = t
				}
			}
		}

		// 7. 递归子树：数据值跳过；容器键只遍历子 Schema，防止属性名（如 required）
		// 被当成 Schema 关键字误删。
		for k, v := range n {
			if schemaDataKeys[k] {
				continue
			}
			if schemaContainerKeys[k] {
				if c, ok := v.(map[string]any); ok {
					for childKey, child := range c {
						if child == nil {
							delete(c, childKey)
							continue
						}
						normalizeSchemaTree(child)
					}
				}
				continue
			}
			normalizeSchemaTree(v)
		}
	case []any:
		for _, v := range n {
			normalizeSchemaTree(v)
		}
	}
}

// inferEnumType 从 enum 取值推断 JSON Schema 类型；推不出唯一类型时返回空（不改写）。
// 数字经 encoding/json 解出来一律是 float64，故按「全为整数」区分 integer / number。
func inferEnumType(values []any) string {
	var strs, nums, ints, bools int
	for _, v := range values {
		switch x := v.(type) {
		case string:
			strs++
		case bool:
			bools++
		case float64:
			nums++
			if x == math.Trunc(x) {
				ints++
			}
		default:
			return "" // null / 对象 / 数组：类型不唯一，不猜
		}
	}
	n := len(values)
	switch {
	case strs == n:
		return "string"
	case bools == n:
		return "boolean"
	case nums == n && ints == n:
		return "integer"
	case nums == n:
		return "number"
	}
	return ""
}

// inlineKimiSchema 把本地 $ref 展开成内联定义（顶层 $ref 用；Kimi 等后端不接受
// parameters 里的 $ref/#/$defs）。带预算与深度上限，循环引用返回空 schema。
func inlineKimiSchema(node any, defs map[string]any, seen map[string]bool, depth int) any {
	budget := 10000
	return inlineKimiSchemaBudget(node, defs, seen, depth, &budget)
}

func inlineKimiSchemaBudget(node any, defs map[string]any, seen map[string]bool, depth int, budget *int) any {
	if *budget <= 0 {
		return nil
	}
	*budget--
	if depth > 64 {
		return map[string]any{}
	}
	switch v := node.(type) {
	case []any:
		out := make([]any, len(v))
		for i, x := range v {
			if *budget <= 0 {
				break
			}
			out[i] = inlineKimiSchemaBudget(x, defs, seen, depth+1, budget)
		}
		return out
	case map[string]any:
		if ref, ok := v["$ref"].(string); ok && strings.HasPrefix(ref, "#/$defs/") {
			name := strings.TrimPrefix(ref, "#/$defs/")
			name = strings.ReplaceAll(strings.ReplaceAll(name, "~1", "/"), "~0", "~")
			if seen[name] {
				return map[string]any{}
			}
			target, ok := defs[name]
			if !ok {
				return map[string]any{"type": "object"}
			}
			next := make(map[string]bool, len(seen)+1)
			for k, x := range seen {
				next[k] = x
			}
			next[name] = true
			return inlineKimiSchemaBudget(target, defs, next, depth+1, budget)
		}
		out := make(map[string]any, len(v))
		for k, x := range v {
			if *budget <= 0 {
				break
			}
			if k != "$defs" && k != "$schema" {
				out[k] = inlineKimiSchemaBudget(x, defs, seen, depth+1, budget)
			}
		}
		return out
	default:
		return node
	}
}
