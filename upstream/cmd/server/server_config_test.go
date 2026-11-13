package main

import (
	"strings"
	"testing"
	"time"
)

// TestServerConfigDefaults server 段默认值：64 MiB / 300s / 30s。
// 这三个值都是「边界」而不是「策略」，所以必须能从配置改，且有明确默认。
func TestServerConfigDefaults(t *testing.T) {
	c := Default()
	if err := c.normalize(); err != nil {
		t.Fatalf("default normalize: %v", err)
	}
	if c.Server.MaxPayloadBytes != 64<<20 {
		t.Errorf("max_payload_bytes 默认 %d，期望 64 MiB", c.Server.MaxPayloadBytes)
	}
	if c.ReadTimeoutDur != 300*time.Second {
		t.Errorf("read_timeout 默认 %v，期望 300s", c.ReadTimeoutDur)
	}
	if c.ShutdownTimeoutDur != 30*time.Second {
		t.Errorf("shutdown_timeout 默认 %v，期望 30s", c.ShutdownTimeoutDur)
	}
}

// TestServerConfigExplicitValues 显式配置必须生效。
func TestServerConfigExplicitValues(t *testing.T) {
	c := Default()
	c.Server.MaxPayloadBytes = 128 << 20
	c.Server.ReadTimeout = "120s"
	c.Server.ShutdownTimeout = "90s"
	if err := c.normalize(); err != nil {
		t.Fatalf("normalize: %v", err)
	}
	if c.Server.MaxPayloadBytes != 128<<20 {
		t.Errorf("max_payload_bytes=%d", c.Server.MaxPayloadBytes)
	}
	if c.ReadTimeoutDur != 120*time.Second {
		t.Errorf("read_timeout=%v", c.ReadTimeoutDur)
	}
	if c.ShutdownTimeoutDur != 90*time.Second {
		t.Errorf("shutdown_timeout=%v", c.ShutdownTimeoutDur)
	}
}

// TestServerConfigZeroTimeoutIsMeaningful 显式 "0" 与「没写」必须是两件事：
// 前者是「不限 / 立即退出」，后者回落默认。把两者混在一起会让语义反转。
func TestServerConfigZeroTimeoutIsMeaningful(t *testing.T) {
	c := Default()
	c.Server.ReadTimeout = "0"
	c.Server.ShutdownTimeout = "0"
	if err := c.normalize(); err != nil {
		t.Fatalf("normalize: %v", err)
	}
	if c.ReadTimeoutDur != 0 {
		t.Errorf("显式 0 应保留为 0（不限），got %v", c.ReadTimeoutDur)
	}
	if c.ShutdownTimeoutDur != 0 {
		t.Errorf("显式 0 应保留为 0（立即退出），got %v", c.ShutdownTimeoutDur)
	}
}

// TestServerConfigPayloadBounds max_payload_bytes 越界必须 fail fast，
// 不能静默钳——一个被悄悄改小的上限会在长会话上偶发 413，极难归因。
func TestServerConfigPayloadBounds(t *testing.T) {
	for _, v := range []int64{1, 1 << 20, 8<<20 - 1, 512<<20 + 1} {
		c := Default()
		c.Server.MaxPayloadBytes = v
		err := c.normalize()
		if err == nil {
			t.Errorf("%d 应报错（允许 8–512 MiB）", v)
			continue
		}
		if !strings.Contains(err.Error(), "server.max_payload_bytes") {
			t.Errorf("%d 的报错没点出键名：%v", v, err)
		}
	}
	// 边界值本身合法。
	for _, v := range []int64{8 << 20, 512 << 20} {
		c := Default()
		c.Server.MaxPayloadBytes = v
		if err := c.normalize(); err != nil {
			t.Errorf("%d 应合法：%v", v, err)
		}
	}
}

// TestServerConfigBadDuration 非法时长与负值都报错，且点出键名。
func TestServerConfigBadDuration(t *testing.T) {
	for _, tc := range []struct{ key, val string }{
		{"server.read_timeout", "不是时长"},
		{"server.read_timeout", "-5s"},
		{"server.shutdown_timeout", "abc"},
		{"server.shutdown_timeout", "-1s"},
	} {
		c := Default()
		if tc.key == "server.read_timeout" {
			c.Server.ReadTimeout = tc.val
		} else {
			c.Server.ShutdownTimeout = tc.val
		}
		err := c.normalize()
		if err == nil {
			t.Errorf("%s=%q 应报错", tc.key, tc.val)
			continue
		}
		if !strings.Contains(err.Error(), tc.key) {
			t.Errorf("%s=%q 的报错没点出键名：%v", tc.key, tc.val, err)
		}
	}
}
