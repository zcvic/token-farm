// bodylimit.go 入站请求体的安全上限 + 慢上传可见性。
//
// 为什么不是「预拦截」：上游自己的 max_body_mb 已因多图/长会话被移除过（大请求交
// 上游自然响应，错误信息更具体）。这里只挡一类情况——大到足以把进程读死的上传：
// io.ReadAll 无界，带 key 的客户端（或跑飞的脚本）一次几百 MB 的 body 就能把网关
// 读成 OOM。上限因此刻意远高于真实会话（长会话多图 base64 实测在几 MB 量级），
// 只当护栏，不当内容策略。
//
// 超限时的两个动作缺一不可：先排空有限字节、再回 413。直接 return 会让 net/http
// 关掉连接，客户端往往只看到 connection reset，看不到那份 413。
package server

import (
	"io"
	"log"
	"net/http"
	"strconv"
	"time"
)

// defaultMaxPayloadBytes 缺省上限。允许范围由配置层校验（server.max_payload_bytes
// 8–512 MiB），这里只兜「没配」。
const defaultMaxPayloadBytes = 64 << 20

// slowBodyWarn 读体超过该时长就打一条 WARN。
//
// 为什么记这个：正常 body 是瞬间到的，读体慢只有一种成因——卡在客户端或中间层的
// 上传/缓冲。这段时间此前完全不可见，于是「上传慢」和「请求根本没到本进程」在
// 日志上无法区分。阈值取 3s：环回/LAN 上正常请求是毫秒级，到秒级已经是异常。
const slowBodyWarn = 3 * time.Second

// bodyReadError 读体失败。TooLarge 时调用方按各自协议的形状回 413，其余按既有的
// 400 invalid_request 处理（与改动前的 "read body: …" 文案一致）。
type bodyReadError struct {
	TooLarge bool
	Limit    int64
	Err      error
}

func (e *bodyReadError) Error() string {
	if e.TooLarge {
		return "request body exceeds " + strconv.FormatInt(e.Limit, 10) + " bytes (server.max_payload_bytes)"
	}
	if e.Err == nil {
		return "read body"
	}
	return "read body: " + e.Err.Error()
}

// readBody 读入请求体并施加安全上限：最多读 limit+1 字节，多出来的那一个字节就是
// 「超限」的证据。声明了 Content-Length 的超大上传与块上传在这里是同一条路径——
// 不去看头，因为头可以撒谎，读进去的字节不会。
func (h *Handler) readBody(w http.ResponseWriter, r *http.Request) ([]byte, *bodyReadError) {
	limit := h.maxPayload()
	start := time.Now()
	body, err := io.ReadAll(io.LimitReader(r.Body, limit+1))
	if wait := time.Since(start); wait > slowBodyWarn {
		// 只记元信息：路径、读了多长、多慢、声明了多少。不含 body 内容。
		log.Printf("WARN: [server] slow request body: %s %s read %d bytes in %s (Content-Length=%d)",
			r.Method, r.URL.Path, len(body), wait.Round(time.Millisecond), r.ContentLength)
	}
	if int64(len(body)) > limit {
		drainBody(w, r, limit)
		return nil, &bodyReadError{TooLarge: true, Limit: limit}
	}
	if err != nil {
		return nil, &bodyReadError{Err: err}
	}
	return body, nil
}

// drainBody 排空剩余上传（读满 limit 字节或 2s 到点即停）。限时是为了不让一个慢速
// 客户端把连接和 goroutine 拖住。
func drainBody(w http.ResponseWriter, r *http.Request, limit int64) {
	_ = http.NewResponseController(w).SetReadDeadline(time.Now().Add(2 * time.Second))
	_, _ = io.CopyN(io.Discard, r.Body, limit)
}

// writeBodyError 按 OpenAI 错误形状回包。
// 本地没有 /v1/messages，所以只有这一种形状；将来接 Anthropic 协议时在此加分支。
func (h *Handler) writeBodyError(w http.ResponseWriter, berr *bodyReadError) {
	status := http.StatusBadRequest
	code := "invalid_request"
	if berr.TooLarge {
		status = http.StatusRequestEntityTooLarge
		code = "payload_too_large"
	}
	writeOpenAIError(w, status, code, berr.Error())
}

// maxPayload 生效上限：配置 > 0 时用配置，否则用缺省 64 MiB。
func (h *Handler) maxPayload() int64 {
	if h.cfg.MaxPayloadBytes > 0 {
		return h.cfg.MaxPayloadBytes
	}
	return defaultMaxPayloadBytes
}
