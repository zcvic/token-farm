package upstream

import (
	"encoding/json"
	"testing"
)

// 面板在权重非 100 / 停用线路时会写对象形；上游必须也能读，否则整份配置解析失败。
func TestProxyTableAcceptsObjectAndString(t *testing.T) {
	var doc struct {
		Proxies ProxyTable `json:"proxies"`
	}
	raw := `{"proxies":{"a":"http://a.example:8080",` +
		`"b":{"url":"socks5h://user:pass@b.example:1080","weight":30,"enabled":false}}}`
	if err := json.Unmarshal([]byte(raw), &doc); err != nil {
		t.Fatalf("对象形线路表解析失败：%v", err)
	}
	if got := doc.Proxies["a"]; got != "http://a.example:8080" {
		t.Errorf("字符串形地址不对：%q", got)
	}
	if got := doc.Proxies["b"]; got != "socks5h://user:pass@b.example:1080" {
		t.Errorf("对象形地址不对：%q", got)
	}
}
