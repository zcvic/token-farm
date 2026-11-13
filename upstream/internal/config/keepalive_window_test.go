package config

import (
	"strings"
	"testing"
	"time"
)

// TestKeepaliveRefreshWindowDefault 空值回落 168h（7 天）。
func TestKeepaliveRefreshWindowDefault(t *testing.T) {
	s := DefaultSchedule()
	if s.KeepaliveRefreshWindow != "168h" {
		t.Fatalf("默认 %q，期望 168h", s.KeepaliveRefreshWindow)
	}
	if err := s.Normalize(); err != nil {
		t.Fatalf("Normalize: %v", err)
	}
	d, err := time.ParseDuration(s.KeepaliveRefreshWindow)
	if err != nil || d != 168*time.Hour {
		t.Fatalf("解析 %v err=%v", d, err)
	}
}

// TestKeepaliveRefreshWindowEmptyFallsBack 显式空串也回落默认。
func TestKeepaliveRefreshWindowEmptyFallsBack(t *testing.T) {
	s := DefaultSchedule()
	s.KeepaliveRefreshWindow = ""
	if err := s.Normalize(); err != nil {
		t.Fatalf("Normalize: %v", err)
	}
	if s.KeepaliveRefreshWindow != "168h" {
		t.Fatalf("空串应回落 168h，got %q", s.KeepaliveRefreshWindow)
	}
}

// TestKeepaliveRefreshWindowRejectsBad 非法与 <= 0 必须 fail fast。
//
// 特别覆盖 "168"（少写单位）：若不校验，ParseDuration 会报错——这是好事，
// 但更危险的是有人为了「关掉窗口」写 0，那会让保活退化成全量刷新且没人发现。
func TestKeepaliveRefreshWindowRejectsBad(t *testing.T) {
	for _, bad := range []string{"abc", "168", "0", "0s", "-1h"} {
		s := DefaultSchedule()
		s.KeepaliveRefreshWindow = bad
		err := s.Normalize()
		if err == nil {
			t.Errorf("%q 应报错", bad)
			continue
		}
		if !strings.Contains(err.Error(), "keepalive_refresh_window") {
			t.Errorf("%q 的报错没点出键名：%v", bad, err)
		}
	}
}

// TestKeepaliveRefreshWindowAcceptsPositive 正常正时长放行。
func TestKeepaliveRefreshWindowAcceptsPositive(t *testing.T) {
	for _, ok := range []string{"24h", "168h", "8760h", "30m"} {
		s := DefaultSchedule()
		s.KeepaliveRefreshWindow = ok
		if err := s.Normalize(); err != nil {
			t.Errorf("%q 应合法：%v", ok, err)
		}
	}
}
