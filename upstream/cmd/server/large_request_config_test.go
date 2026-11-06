package main

import (
	"strings"
	"testing"
)

// TestLargeRequestModeConfig 大请求选号策略的缺省与校验：
//   - 缺省（老配置没写这个键）= sticky，缓存优先；
//   - rotate 是显式逃生门；
//   - 写错必须启动报错，不能静默回落成 sticky —— 否则用户以为切到了 rotate，
//     实际还在缓存模式，上游风控行为与预期不符。
func TestLargeRequestModeConfig(t *testing.T) {
	c := Default()
	if err := c.normalize(); err != nil {
		t.Fatalf("default normalize: %v", err)
	}
	if c.Pool.LargeRequestMode != "sticky" {
		t.Errorf("默认 large_request_mode=%q，want sticky", c.Pool.LargeRequestMode)
	}

	c = Default()
	c.Pool.LargeRequestMode = "rotate"
	if err := c.normalize(); err != nil {
		t.Fatalf("rotate normalize: %v", err)
	}
	if c.Pool.LargeRequestMode != "rotate" {
		t.Errorf("large_request_mode=%q，want rotate", c.Pool.LargeRequestMode)
	}

	c = Default()
	c.Pool.LargeRequestMode = "rotate-everything"
	err := c.normalize()
	if err == nil {
		t.Fatal("非法 large_request_mode 应报错")
	}
	if !strings.Contains(err.Error(), "pool.large_request_mode") {
		t.Errorf("错误信息未点名配置键：%v", err)
	}
}
