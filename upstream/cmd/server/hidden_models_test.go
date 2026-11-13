package main

import (
	"os"
	"path/filepath"
	"testing"
)

// TestHiddenProviderReads 基本读取：从 config.json 的 models.hidden 取名单。
func TestHiddenProviderReads(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(`{"models":{"hidden":["glm-5.2","cn:qwen-3"]}}`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	h := newHiddenProvider(p)
	got := h.Hidden()
	if len(got) != 2 || got[0] != "glm-5.2" || got[1] != "cn:qwen-3" {
		t.Fatalf("got %v", got)
	}
}

// TestHiddenProviderMissingFileIsEmpty 文件不存在是合法形态（纯默认部署）。
func TestHiddenProviderMissingFileIsEmpty(t *testing.T) {
	h := newHiddenProvider(filepath.Join(t.TempDir(), "nope.json"))
	if got := h.Hidden(); len(got) != 0 {
		t.Fatalf("got %v", got)
	}
}

// TestHiddenProviderNilSafe 未接线（path 空 / nil）时返回 nil。
func TestHiddenProviderNilSafe(t *testing.T) {
	if got := newHiddenProvider("").Hidden(); got != nil {
		t.Fatalf("空 path 应返回 nil，got %v", got)
	}
	var nilProv *hiddenProvider
	if got := nilProv.Hidden(); got != nil {
		t.Fatalf("nil provider 应返回 nil，got %v", got)
	}
}

// TestHiddenProviderKeepsLastGoodOnParseError 解析失败必须沿用上一份好值，
// **不能**清空成「什么都没藏」——那等于一次写盘抖动把整份名单放开。
func TestHiddenProviderKeepsLastGoodOnParseError(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(`{"models":{"hidden":["glm-5.2"]}}`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	h := newHiddenProvider(p)
	if got := h.Hidden(); len(got) != 1 {
		t.Fatalf("首次读取 %v", got)
	}
	// 写坏文件，并绕过 TTL 强制重读。
	if err := os.WriteFile(p, []byte(`{"models":`), 0o600); err != nil {
		t.Fatalf("corrupt: %v", err)
	}
	h.mu.Lock()
	h.readAt = h.readAt.Add(-2 * hiddenReloadTTL)
	h.mu.Unlock()
	got := h.Hidden()
	if len(got) != 1 || got[0] != "glm-5.2" {
		t.Fatalf("解析失败后应沿用上一份好值，got %v", got)
	}
}

// TestHiddenProviderKeepsLastGoodOnReadError 文件暂时读不到（权限等）同样沿用旧值。
func TestHiddenProviderKeepsLastGoodOnReadError(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(`{"models":{"hidden":["a"]}}`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	h := newHiddenProvider(p)
	h.Hidden()
	// 删除文件：os.IsNotExist 分支也走「沿用旧值」，保证不会因文件被临时挪走而放开名单。
	if err := os.Remove(p); err != nil {
		t.Fatalf("remove: %v", err)
	}
	h.mu.Lock()
	h.readAt = h.readAt.Add(-2 * hiddenReloadTTL)
	h.mu.Unlock()
	got := h.Hidden()
	if len(got) != 1 || got[0] != "a" {
		t.Fatalf("读失败后应沿用上一份好值，got %v", got)
	}
}

// TestCleanHidden 归一化：去空白、去空串、去重（大小写不敏感）。
func TestCleanHidden(t *testing.T) {
	got := cleanHidden([]string{"  glm-5.2  ", "", "GLM-5.2", "cn:qwen", "   "})
	if len(got) != 2 || got[0] != "glm-5.2" || got[1] != "cn:qwen" {
		t.Fatalf("got %v", got)
	}
}

// TestHiddenProviderReturnsCopy 返回值必须是副本：调用方改动不能污染缓存。
func TestHiddenProviderReturnsCopy(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	if err := os.WriteFile(p, []byte(`{"models":{"hidden":["a"]}}`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	h := newHiddenProvider(p)
	first := h.Hidden()
	first[0] = "tampered"
	second := h.Hidden()
	if second[0] != "a" {
		t.Fatalf("缓存被外部改动污染：%v", second)
	}
}
