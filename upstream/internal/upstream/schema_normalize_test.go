package upstream

import (
	"encoding/json"
	"reflect"
	"testing"
)

// normalizeParamsJSON 把一段 parameters JSON 走一遍 normalizeToolParameters。
func normalizeParamsJSON(t *testing.T, src string) map[string]any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(src), &v); err != nil {
		t.Fatalf("夹具不是合法 JSON: %v", err)
	}
	out, ok := normalizeToolParameters(v).(map[string]any)
	if !ok {
		t.Fatalf("结果不是 object: %#v", out)
	}
	return out
}

func schemaMustJSON(t *testing.T, src string) any {
	t.Helper()
	var v any
	if err := json.Unmarshal([]byte(src), &v); err != nil {
		t.Fatalf("夹具不是合法 JSON: %v", err)
	}
	return v
}

func schemaPropOf(t *testing.T, params map[string]any, name string) map[string]any {
	t.Helper()
	props, _ := params["properties"].(map[string]any)
	p, ok := props[name].(map[string]any)
	if !ok {
		t.Fatalf("属性 %s 丢了: %#v", name, props)
	}
	return p
}

// TestNormalizeSchemaKeepsDataKeywords default / const / enum / example(s) 的值是数据，
// 不是 schema：里面出现 type/items/required/null 这些键也必须原样保留，否则改的是
// 客户端声明的取值本身（模型看到的默认值、常量都变了）。
func TestNormalizeSchemaKeepsDataKeywords(t *testing.T) {
	params := normalizeParamsJSON(t, `{"type":"object","properties":{
		"mode":{"type":"object","default":{"type":"str","items":["a"],"required":"x","note":null}},
		"tags":{"type":"array","items":{"type":"string"},"examples":[{"required":[null,""]}]},
		"kind":{"const":{"type":"int"}},
		"shape":{"enum":[{"type":"list","items":[1]}]}
	}}`)
	cases := map[string]struct{ key, want string }{
		"mode":  {"default", `{"type":"str","items":["a"],"required":"x","note":null}`},
		"tags":  {"examples", `[{"required":[null,""]}]`},
		"kind":  {"const", `{"type":"int"}`},
		"shape": {"enum", `[{"type":"list","items":[1]}]`},
	}
	for prop, c := range cases {
		got := schemaPropOf(t, params, prop)[c.key]
		if want := schemaMustJSON(t, c.want); !reflect.DeepEqual(got, want) {
			t.Errorf("%s.%s 被改写: got %#v want %#v", prop, c.key, got, want)
		}
	}
}

// TestNormalizeSchemaDependencyContainers dependentRequired / dependencies 的键是属性名
// （数据），值是属性名数组或子 schema：键叫 items/type 也不能当 schema 关键字处理，
// 子 schema 照常规整。
func TestNormalizeSchemaDependencyContainers(t *testing.T) {
	params := normalizeParamsJSON(t, `{"type":"object",
		"properties":{"items":{"type":"string"},"type":{"type":"string"},"z":{"type":"string"}},
		"dependentRequired":{"items":["type"]},
		"dependencies":{"type":["items"],"z":{"type":"str"}}
	}`)
	if got, want := params["dependentRequired"], schemaMustJSON(t, `{"items":["type"]}`); !reflect.DeepEqual(got, want) {
		t.Errorf("dependentRequired 被改写: got %#v want %#v", got, want)
	}
	deps, _ := params["dependencies"].(map[string]any)
	if got, want := deps["type"], schemaMustJSON(t, `["items"]`); !reflect.DeepEqual(got, want) {
		t.Errorf("dependencies.type 被改写: got %#v want %#v", got, want)
	}
	if z, _ := deps["z"].(map[string]any); z["type"] != "string" {
		t.Errorf("dependencies 里的子 schema 没有规整: %#v", deps["z"])
	}
}

// TestNormalizeSchemaKeepsNullValues const:null / default:null 是有意义的取值（「只能是
// null」「缺省为 null」），不是缺失的元数据；description:null 这类元数据照旧剔除。
func TestNormalizeSchemaKeepsNullValues(t *testing.T) {
	params := normalizeParamsJSON(t, `{"type":"object","properties":{
		"x":{"const":null,"description":null},
		"y":{"type":["string","null"],"default":null,"title":null}
	}}`)
	x := schemaPropOf(t, params, "x")
	if v, ok := x["const"]; !ok || v != nil {
		t.Errorf("const:null 被删: %#v", x)
	}
	if _, ok := x["description"]; ok {
		t.Errorf("description:null 应剔除: %#v", x)
	}
	y := schemaPropOf(t, params, "y")
	if v, ok := y["default"]; !ok || v != nil {
		t.Errorf("default:null 被删: %#v", y)
	}
	if _, ok := y["title"]; ok {
		t.Errorf("title:null 应剔除: %#v", y)
	}
}

// TestCoerceSchemaKeywordTypes 上游按 Go 强类型反序列化 schema：字符串形态的数值/布尔
// 关键字、数字形态的字符串关键字、非数组的 enum、非布尔的 additionalProperties 都整体
// 400（code=11129）。纠不动就删键——留着非法值等于保证整颗工具必然被拒。
func TestCoerceSchemaKeywordTypes(t *testing.T) {
	body := `{"model":"m","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function",` +
		`"function":{"name":"f","parameters":{"type":"object","properties":{` +
		`"n":{"type":"integer","minimum":"1","maximum":"abc"},` +
		`"c":{"type":"array","items":{"type":"string"},"uniqueItems":"true"},` +
		`"d":{"type":"string","description":123},` +
		`"e":{"type":"string","enum":"x"},` +
		`"o":{"type":"object","properties":{},"additionalProperties":"false"}},` +
		`"required":["n","n","c"]}}}]}`
	var obj map[string]any
	if err := json.Unmarshal(PrepareBodyOpt([]byte(body), false), &obj); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	params := obj["tools"].([]any)[0].(map[string]any)["function"].(map[string]any)["parameters"].(map[string]any)
	props := params["properties"].(map[string]any)

	if got := props["n"].(map[string]any)["minimum"]; got != float64(1) {
		t.Errorf("minimum = %v (%T), want 1 (number)", got, got)
	}
	if _, ok := props["n"].(map[string]any)["maximum"]; ok {
		t.Errorf(`maximum "abc" should be dropped, got %v`, props["n"])
	}
	if got := props["c"].(map[string]any)["uniqueItems"]; got != true {
		t.Errorf("uniqueItems = %v (%T), want true (bool)", got, got)
	}
	if _, ok := props["d"].(map[string]any)["description"]; ok {
		t.Errorf("numeric description should be dropped, got %v", props["d"])
	}
	if _, ok := props["e"].(map[string]any)["enum"]; ok {
		t.Errorf("non-array enum should be dropped, got %v", props["e"])
	}
	if got := props["o"].(map[string]any)["additionalProperties"]; got != false {
		t.Errorf("additionalProperties = %v (%T), want false (bool)", got, got)
	}
	// required 重复项同样 400 11129（真机：["a","a"]），必须去重。
	req := params["required"]
	if got, ok := req.([]any); !ok || len(got) != 2 || got[0] != "n" || got[1] != "c" {
		t.Errorf("required = %v, want [n c] (deduped)", req)
	}
}

// TestTopLevelRefInlinedOrDropped 顶层 $ref 实测 400 11129（嵌套 $ref + $defs 反而 200），
// 所以只处理顶层：能按 $defs 展开就保住真实参数，展不开也必须删掉。
func TestTopLevelRefInlinedOrDropped(t *testing.T) {
	params := func(body string) map[string]any {
		t.Helper()
		var obj map[string]any
		if err := json.Unmarshal(PrepareBodyOpt([]byte(body), false), &obj); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		return obj["tools"].([]any)[0].(map[string]any)["function"].(map[string]any)["parameters"].(map[string]any)
	}

	t.Run("有 $defs 时展开", func(t *testing.T) {
		p := params(`{"model":"m","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function",` +
			`"function":{"name":"f","parameters":{"$ref":"#/$defs/Foo",` +
			`"$defs":{"Foo":{"type":"object","properties":{"x":{"type":"string"}}}}}}}]}`)
		if _, ok := p["$ref"]; ok {
			t.Fatalf("$ref should be gone, got %v", p)
		}
		props, _ := p["properties"].(map[string]any)
		if _, ok := props["x"]; !ok {
			t.Fatalf("inlined properties missing, got %v", p)
		}
	})

	t.Run("无 $defs 时只删 ref", func(t *testing.T) {
		p := params(`{"model":"m","messages":[{"role":"user","content":"hi"}],"tools":[{"type":"function",` +
			`"function":{"name":"f","parameters":{"$ref":"#/$defs/Foo"}}}]}`)
		if _, ok := p["$ref"]; ok {
			t.Fatalf("$ref should be gone, got %v", p)
		}
		if p["type"] != "object" {
			t.Fatalf("type = %v, want object", p["type"])
		}
	})
}

// TestNormalizeSchemaLegacyFunctions 旧式 functions[] 与 tools[] 同口径（本地既有契约），
// pattern 转义与 enum 补 type 都要生效。
func TestNormalizeSchemaLegacyFunctions(t *testing.T) {
	body := `{"model":"m","messages":[{"role":"user","content":"hi"}],"functions":[{"name":"f",` +
		`"parameters":{"type":"object","properties":{` +
		`"id":{"pattern":"^agent\\_run\\_"},` +
		`"mode":{"enum":["a","b"]}}}}]}`
	var obj map[string]any
	if err := json.Unmarshal(PrepareBodyOpt([]byte(body), false), &obj); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	params := obj["functions"].([]any)[0].(map[string]any)["parameters"].(map[string]any)
	props := params["properties"].(map[string]any)
	if got := props["id"].(map[string]any)["pattern"]; got != "^agent_run_" {
		t.Errorf("pattern = %v, want ^agent_run_", got)
	}
	if got := props["mode"].(map[string]any)["type"]; got != "string" {
		t.Errorf("enum 缺 type 没补上: %v", got)
	}
}
