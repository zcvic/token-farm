// hidden_models.go 全局隐藏名单的热加载（config.json 的 models.hidden）。
//
// 为什么是热加载而不是启动时读一次：这份名单由 Python 面板的「模型可见性」设置写入
// config.json（见 server/services/modelpolicy.py 的 save）。如果 Go 只在启动时读，
// 用户在面板上藏掉一个模型后，直连上游端口仍然看得到、调得通——「藏了但没生效」
// 是最难排查的一类问题（界面显示已隐藏，行为却不是）。
//
// 读法照抄 device_token 的形态：TTL 缓存 + 读失败优雅降级（保留上一份好值，
// 绝不清空成「什么都没藏」——那等于一次写盘抖动就把整份名单放开）。
package main

import (
	"encoding/json"
	"log"
	"os"
	"strings"
	"sync"
	"time"
)

// hiddenReloadTTL 隐藏名单的重读间隔。面板保存后最迟这么久生效。
// 取值与 Python 侧 realm.py 的 10 秒配置缓存同量级：够快，又不至于每个请求都读盘。
const hiddenReloadTTL = 10 * time.Second

// hiddenProvider 带 TTL 缓存的 models.hidden 读取器。
type hiddenProvider struct {
	path string

	mu     sync.Mutex
	list   []string
	readAt time.Time
	loaded bool
	lastOK []string // 最近一次成功读到的值；读失败时保留它，不清空
}

// newHiddenProvider 构造读取器。path 为空表示不读文件（纯 env/默认部署）。
func newHiddenProvider(path string) *hiddenProvider {
	return &hiddenProvider{path: path}
}

// Hidden 返回当前隐藏名单（副本，调用方随意持有）。
//
// 读失败时的降级策略：返回**上一份成功读到的值**，而不是空名单。理由——空名单等于
// 「什么都没藏」，会把用户明确藏掉的模型一次性全放出去；保留旧值最多让变更延迟生效。
func (p *hiddenProvider) Hidden() []string {
	if p == nil || p.path == "" {
		return nil
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.loaded && time.Since(p.readAt) < hiddenReloadTTL {
		return append([]string(nil), p.lastOK...)
	}
	p.readAt = time.Now()
	p.loaded = true
	raw, err := os.ReadFile(p.path)
	if err != nil {
		// 文件不存在是合法形态（纯默认部署）；其他错误值得留痕。
		if !os.IsNotExist(err) {
			log.Printf("WARN: [server] 读取隐藏名单失败，沿用上一份（%d 条）: %v", len(p.lastOK), err)
		}
		p.list = append([]string(nil), p.lastOK...)
		return append([]string(nil), p.list...)
	}
	var doc struct {
		Models struct {
			Hidden []string `json:"hidden"`
		} `json:"models"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		log.Printf("WARN: [server] 解析 config.json 失败，隐藏名单沿用上一份（%d 条）: %v", len(p.lastOK), err)
		p.list = append([]string(nil), p.lastOK...)
		return append([]string(nil), p.list...)
	}
	p.lastOK = cleanHidden(doc.Models.Hidden)
	p.list = append([]string(nil), p.lastOK...)
	return append([]string(nil), p.list...)
}

// cleanHidden 归一化名单：去空白、去空串、去重（大小写不敏感），保留原写法。
// 与 Python modelpolicy._clean_hidden 同口径——两边对同一份名单的理解必须一致。
func cleanHidden(raw []string) []string {
	out := make([]string, 0, len(raw))
	seen := make(map[string]bool, len(raw))
	for _, item := range raw {
		text := strings.TrimSpace(item)
		if text == "" {
			continue
		}
		key := strings.ToLower(text)
		if seen[key] {
			continue
		}
		seen[key] = true
		out = append(out, text)
	}
	return out
}
