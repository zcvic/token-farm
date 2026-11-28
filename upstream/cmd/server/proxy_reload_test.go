package main

import (
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"testing"

	"workbuddy2api/internal/upstream"
)

// issue #379：面板改 config.json 的 proxies 段后，上游不重启也要能解析新线路；
// 解析失败时保留旧表。
func TestReloadProxyConfig(t *testing.T) {
	upstream.ConfigureProxies(nil)
	t.Cleanup(func() { upstream.ConfigureProxies(nil) })

	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	write := func(s string) {
		t.Helper()
		if err := os.WriteFile(path, []byte(s), 0o600); err != nil {
			t.Fatal(err)
		}
	}

	write(`{"proxies":{"route-a":"socks5h://user:pass@a.example:1080"}}`)
	if !reloadProxyConfig(path) {
		t.Fatal("首次热重载失败")
	}
	if names := upstream.RouteNames(); len(names) != 1 || names[0] != "route-a" {
		t.Fatalf("线路表不对：%v", names)
	}
	tr, err := upstream.TransportFor("route-a")
	if err != nil {
		t.Fatal(err)
	}
	u, err := tr.Proxy(&http.Request{URL: &url.URL{Scheme: "https", Host: "api.example"}})
	if err != nil || u == nil || u.Host != "a.example:1080" {
		t.Fatalf("route-a 代理不对：%v %v", u, err)
	}

	// 改地址：必须重建 transport，不能复用旧代理连接池。
	write(`{"proxies":{"route-a":"http://b.example:8080"}}`)
	if !reloadProxyConfig(path) {
		t.Fatal("第二次热重载失败")
	}
	tr2, err := upstream.TransportFor("route-a")
	if err != nil {
		t.Fatal(err)
	}
	u2, err := tr2.Proxy(&http.Request{URL: &url.URL{Scheme: "https", Host: "api.example"}})
	if err != nil || u2 == nil || u2.Host != "b.example:8080" {
		t.Fatalf("改地址后代理没更新：%v %v", u2, err)
	}

	// 坏配置：返回失败，旧线路仍在（不能把正在服务的表清空）。
	write(`{"proxies":`)
	if reloadProxyConfig(path) {
		t.Fatal("坏配置应返回 false")
	}
	if names := upstream.RouteNames(); len(names) != 1 || names[0] != "route-a" {
		t.Fatalf("坏配置把旧线路表清空了：%v", names)
	}
}

// 面板对象形线路表也要能通过 config 解析（与 internal/upstream 的兼容层同一口径）。
func TestConfigAcceptsProxyObjectForm(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.json")
	raw := `{"proxies":{"route-a":{"url":"http://a.example:8080","weight":30,"enabled":false}}}`
	if err := os.WriteFile(path, []byte(raw), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := Load(path)
	if err != nil {
		t.Fatalf("对象形 proxies 解析失败：%v", err)
	}
	if got := cfg.Proxies["route-a"]; got != "http://a.example:8080" {
		t.Errorf("proxies[route-a]=%q", got)
	}
}
