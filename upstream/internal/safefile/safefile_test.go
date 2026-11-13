package safefile

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestWriteReadRoundTrip 基本写读：写进去什么就读出什么。
func TestWriteReadRoundTrip(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	if err := Write(p, []byte(`{"a":1}`), 0o600); err != nil {
		t.Fatalf("Write: %v", err)
	}
	got, err := Read(p, nil)
	if err != nil {
		t.Fatalf("Read: %v", err)
	}
	if string(got) != `{"a":1}` {
		t.Fatalf("got %q", got)
	}
}

// TestWriteKeepCreatesBak WriteKeep 必须把上一份有效内容留成 .bak。
func TestWriteKeepCreatesBak(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	if err := WriteKeep(p, []byte(`{"gen":1}`), 0o600); err != nil {
		t.Fatalf("first WriteKeep: %v", err)
	}
	// 首次写入时文件不存在：不该产生 .bak。
	if _, err := os.Stat(Bak(p)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("首次写入不应产生 .bak，err=%v", err)
	}
	if err := WriteKeep(p, []byte(`{"gen":2}`), 0o600); err != nil {
		t.Fatalf("second WriteKeep: %v", err)
	}
	bak, err := os.ReadFile(Bak(p))
	if err != nil {
		t.Fatalf("read bak: %v", err)
	}
	if string(bak) != `{"gen":1}` {
		t.Fatalf("bak got %q，期望上一代内容", bak)
	}
}

// TestReadFallsBackToBak 主文件损坏时读 .bak，且原文件不被改动。
func TestReadFallsBackToBak(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	if err := WriteKeep(p, []byte(`{"gen":1}`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := WriteKeep(p, []byte(`{"gen":2}`), 0o600); err != nil {
		t.Fatalf("rotate: %v", err)
	}
	// 模拟掉电留下的全 0 文件。
	if err := os.WriteFile(p, []byte(strings.Repeat("\x00", 32)), 0o600); err != nil {
		t.Fatalf("corrupt: %v", err)
	}
	got, err := Read(p, nil)
	if err != nil {
		t.Fatalf("Read 应回退 .bak，err=%v", err)
	}
	if string(got) != `{"gen":1}` {
		t.Fatalf("回退内容 got %q", got)
	}
	// 原文件必须保持损坏状态（读不修复，写才修复）。
	raw, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if len(raw) != 32 {
		t.Fatalf("Read 不应改写主文件，len=%d", len(raw))
	}
}

// TestWriteKeepQuarantinesBadContent 现有内容已损坏时另存 .bad-*，不污染 .bak。
func TestWriteKeepQuarantinesBadContent(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	if err := os.WriteFile(p, []byte(`{"broken":`), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	if err := WriteKeep(p, []byte(`{"gen":1}`), 0o600); err != nil {
		t.Fatalf("WriteKeep: %v", err)
	}
	if _, err := os.Stat(Bak(p)); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("坏内容不应写进 .bak，err=%v", err)
	}
	matches, err := filepath.Glob(p + ".bad-*")
	if err != nil {
		t.Fatalf("glob: %v", err)
	}
	if len(matches) != 1 {
		t.Fatalf("期望一个 .bad-* 隔离文件，got %v", matches)
	}
}

// TestReadMissingFileIsNotExist 文件不存在时返回 fs.ErrNotExist，且不去 .bak 复活。
func TestReadMissingFileIsNotExist(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	// 只有 .bak，主文件被「特意删掉」。
	if err := Write(Bak(p), []byte(`{"gen":1}`), 0o600); err != nil {
		t.Fatalf("seed bak: %v", err)
	}
	if _, err := Read(p, nil); !errors.Is(err, fs.ErrNotExist) {
		t.Fatalf("期望 fs.ErrNotExist，got %v", err)
	}
}

// TestReadBothBrokenErrors 两边都坏必须报错，且错误里带路径与坏法。
func TestReadBothBrokenErrors(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "state.json")
	if err := os.WriteFile(p, []byte(`not json`), 0o600); err != nil {
		t.Fatalf("seed main: %v", err)
	}
	if err := os.WriteFile(Bak(p), []byte(`also not json`), 0o600); err != nil {
		t.Fatalf("seed bak: %v", err)
	}
	_, err := Read(p, nil)
	if err == nil {
		t.Fatal("两边都坏时必须报错")
	}
	if !strings.Contains(err.Error(), p) {
		t.Fatalf("错误应带路径，got %v", err)
	}
	if !strings.Contains(err.Error(), "不是合法 JSON") {
		t.Fatalf("错误应说明坏法，got %v", err)
	}
}

// TestReadCustomValidator 自定义 valid 覆盖默认的 json.Valid。
func TestReadCustomValidator(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "blob")
	if err := Write(p, []byte("hello"), 0o600); err != nil {
		t.Fatalf("seed: %v", err)
	}
	got, err := Read(p, func(b []byte) bool { return string(b) == "hello" })
	if err != nil || string(got) != "hello" {
		t.Fatalf("got %q err=%v", got, err)
	}
	if _, err := Read(p, func(b []byte) bool { return false }); err == nil {
		t.Fatal("valid 恒 false 且无 .bak 时必须报错")
	}
}
