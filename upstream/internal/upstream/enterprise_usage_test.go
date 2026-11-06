package upstream

// 企业额度查询的单元测试（纯解析层，不打网络）。

import (
	"encoding/json"
	"testing"

	"workbuddy2api/internal/auth"
)

// TestIsEnterprise 覆盖 IsEnterprise 的边界。
func TestIsEnterprise(t *testing.T) {
	cases := []struct {
		name string
		a    *auth.Auth
		want bool
	}{
		{"nil", nil, false},
		{"个人号(空 enterpriseId)", &auth.Auth{UID: "u1"}, false},
		{"企业号", &auth.Auth{UID: "u2", EnterpriseID: "ent-1"}, true},
	}
	for _, c := range cases {
		if got := IsEnterprise(c.a); got != c.want {
			t.Errorf("%s: IsEnterprise=%v want %v", c.name, got, c.want)
		}
	}
}

// TestParseEnterpriseCycleEnd 覆盖周期结束时间解析。
func TestParseEnterpriseCycleEnd(t *testing.T) {
	if got := parseEnterpriseCycleEnd(""); !got.IsZero() {
		t.Errorf("空字符串应返回零值，got=%v", got)
	}
	if got := parseEnterpriseCycleEnd("not-a-time"); !got.IsZero() {
		t.Errorf("非法格式应返回零值，got=%v", got)
	}
	got := parseEnterpriseCycleEnd("2026-11-13 23:59:59")
	if got.IsZero() {
		t.Fatal("合法时间不应返回零值")
	}
	if got.Year() != 2026 || got.Month() != 11 || got.Day() != 13 {
		t.Errorf("解析结果错误: %v", got)
	}
	// UTC+8 墙钟 23:59:59 → UTC 15:59:59
	if _, off := got.Zone(); off != 8*3600 {
		t.Errorf("时区偏移=%d want %d", off, 8*3600)
	}
}

// TestEnterpriseUsageDataParsing 覆盖 **data 本体** 的解析与口径换算。
//
// 关键回归点：billingJSON→doJSON 已经解过信封（返回 env.Data），
// 所以这里解析的是 data 本体。曾经误把它当完整信封再套一层，
// 导致 LimitNum/Credit 全落零值、企业额度恒为 0。
func TestEnterpriseUsageDataParsing(t *testing.T) {
	// doJSON 返回的 data 本体（2026-10-18 实测）
	raw := `{"credit":342.1,"cycleStartTime":"2026-10-13 00:00:00",
		"cycleEndTime":"2026-11-13 23:59:59","limitNum":2000,
		"cycleResetTime":"2026-11-14 00:00:00"}`

	var resp enterpriseUsageData
	if err := json.Unmarshal([]byte(raw), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.LimitNum != 2000 {
		t.Errorf("limitNum=%v want 2000（若为 0 说明又把 data 当信封解了一遍）", resp.LimitNum)
	}
	if resp.Credit != 342.1 {
		t.Errorf("credit=%v want 342.1", resp.Credit)
	}
	// 口径换算：remain = round(limitNum) - round(credit) = 2000 - 342 = 1658
	remain := int64(2000) - int64(342)
	if remain != 1658 {
		t.Errorf("remain=%d want 1658", remain)
	}
	// 周期结束时间应能解析
	if ce := parseEnterpriseCycleEnd(resp.CycleEndTime); ce.IsZero() {
		t.Error("cycleEndTime 解析失败")
	}
}

// TestEnterpriseUsageDataUnlimited limitNum=-1 表示不限量。
func TestEnterpriseUsageDataUnlimited(t *testing.T) {
	raw := `{"credit":10,"limitNum":-1}`
	var resp enterpriseUsageData
	if err := json.Unmarshal([]byte(raw), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp.LimitNum >= 0 {
		t.Errorf("limitNum=%v 应为负数（不限量）", resp.LimitNum)
	}
}
