package main

import "testing"

// TestExampleConfigLoads 钉住「仓库里那份 config.example.json 一定能被 Load 读进来」。
//
// 为什么值得单独一条测试：示例配置是用户部署时的照抄对象。新加的配置键如果只写进
// 结构体、忘了写进示例（或者写进去但格式不对），用户照抄示例就起不来，而这在
// 开发机上永远复现不了。放在这里，任何一次 go test 都会替用户先试一遍。
func TestExampleConfigLoads(t *testing.T) {
	c, err := Load("../../config.example.json")
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if c.Server.MaxPayloadBytes != 64<<20 {
		t.Fatalf("max_payload_bytes=%d", c.Server.MaxPayloadBytes)
	}
	if c.KeepaliveRefreshWindowDur.Hours() != 168 {
		t.Fatalf("keepalive window=%v", c.KeepaliveRefreshWindowDur)
	}
	if c.ReadTimeoutDur.Seconds() != 300 {
		t.Fatalf("read_timeout=%v", c.ReadTimeoutDur)
	}
	if c.ShutdownTimeoutDur.Seconds() != 30 {
		t.Fatalf("shutdown_timeout=%v", c.ShutdownTimeoutDur)
	}
}
