# Web 搜索透传（`/v1/tools/search`）

> 配套实现：`upstream/internal/server/tool_search.go`、
> `upstream/internal/upstream/agenttool_search.go`（Go 网关侧）与
> `server/routers/gateway.py`（面板侧转发）。来源是上游 manager 仓的
> `0057-web-search-tool` 补丁；本仓库没有 `dev/upstream-patches` 目录，
> 代码直接落在 `upstream/` 下，同样随包分发。

## 这是什么

把下游客户端的 web search 请求**原样转发**到上游网关的 agenttool 搜索端点
（`https://www.workbuddy.cn/agenttool/v1/search`）。面板在这里只做代理：

```text
下游（DSH / 任意 OpenAI 兼容客户端，wbk_ 密钥）
   │  POST {面板}:7864/v1/tools/search
   ▼
面板（server/routers/gateway.py）
   │  鉴权（_authorize）→ 按密钥解析上游（_upstream_for）→ 换上游 api_key 转发 → 记账
   ▼
wb2api-go（upstream/，:7863）
   │  POST {wb2api-go}/v1/tools/search
   ▼ 账号池轮转（无粘性）→ 15001 配额软冷却换号 → 官方指纹头组
workbuddy.cn/agenttool/v1/search
```

价值是**一把密钥走全部数据面**：下游用同一把 `wbk_` 密钥既调模型
（`/v1/chat/completions`）又调搜索，不用为搜索另立凭证；搜索调用同样进面板的
调用日志、密钥配额与多分组隔离体系。

## 协议

### 请求

```http
POST /v1/tools/search
Authorization: Bearer <wbk_ 密钥>
Content-Type: application/json
```

```json
{
  "query": "搜索词（必填）",
  "max_results": 5,
  "allowed_domains": ["go.dev"],
  "blocked_domains": ["reddit.com"],
  "freshness": "d1"
}
```

| 字段 | 说明 |
|---|---|
| `query` | 必填；去掉首尾空白后为空 → 400 |
| `max_results` | 可选，默认 5；超过 20 按 20 处理，`<= 0` 按 5 处理 |
| `allowed_domains` | 可选，原样转给上游 |
| `blocked_domains` | 可选；网关层拼成 `" -site:x"` 追加到 `query` 文本（官方 CLI 的语义），调用方不用自己拼 |
| `freshness` | 可选：`d1`（一天）/ `m1`（一月）/ `y1`（一年）；别的值 → 400 |

### 响应（上游信封原样透传）

```json
{
  "code": 0,
  "msg": "",
  "results": [{"title": "...", "url": "...", "snippet": "..."}],
  "total_results": 3,
  "response_time_ms": 812,
  "provider": "bing"
}
```

网关**不做结果过滤**：`code != 0` 但又不是配额耗尽的业务码，照样原样回给
客户端（与 chat 的错误透传同一哲学）——过滤与否由调用方决定。

## 错误语义

| 情况 | 面板 / 网关返回 |
|---|---|
| 密钥缺失、无效、IP 受限、超频 | 与 chat 同一套 401 / 403 / 429，不进上游 |
| `query` 为空或请求体不是 JSON | 400（`invalid_body` / `empty_query`） |
| 上游判定请求本身有问题（参数错等） | 400，带上游 `code` 与 `msg`，**不罚号、不轮转** |
| 单个账号 `15001`（web search 日配额） | 该号**软冷却 10 分钟**后换号重试 |
| 所有可用账号都 15001 | 429，`{"error": "...", "code": "quota_web_search", "tried": N, "detail": "..."}` |
| 账号级故障（限流 / 风控 / 5xx / 会话失效） | 按既有错误策略冷却并换号 |
| 池里没有可用账号 | 503，`{"error": "no account available for web search", "detail": "..."}` |
| 面板连不上上游 | 502 `upstream_unavailable` |

几个刻意的取舍：

- **不建会话粘性**：搜索没有上下文，粘住一个号会很快把它当天的搜索配额打穿。
  每次请求纯轮换。
- **`15001` 用固定 10 分钟软冷却**，不走请求限流那套 streak 退避：搜索配额
  与请求频率无关，短冷却让上游日初重置后的号更早回池；持续 15001 的号会被
  后续请求反复冷却，自然收敛到「当天不可用」。
- **国际版账号直接跳过**：上游没有公开的 agenttool 国际域，不去拿生产账号试错。
- 单次搜索的硬超时 20 秒（对齐官方 CLI 的 `AbortController` 约定）。

## 面板侧的记账与隔离

- 鉴权与 chat 同一管线；版本 / 模型白名单按「不带 model 的数据面调用」口径
  （`is_model_list=True`）放行——否则限定版本的密钥会连搜索都用不了。
  账号池的版本隔离仍由上游按账号侧把关。
- 上游选择走 `_upstream_for`：密钥绑定哪个分组，搜索就只走那个分组的账号。
- 每次调用落一条请求日志：状态码、耗时、错误摘要；Token 记 `0 / 0`
  （上游 agenttool 不返回 usage），与 `/v1/models`、`/v1/usage` 的口径一致。
- 请求 / 响应内容**不做留存**，只记元数据（与全站隐私口径一致）。

## 验证

```bash
# Go 侧（upstream/ 下）
go test ./internal/server -run 'ToolSearch' -count=1

# 面板侧
python -m unittest server.tests.test_gateway_tools_search -v
```

Go 侧覆盖：鉴权、空 query、非法 freshness、blocked 域拼接、15001 换号、
全池 15001 → 429、软冷却落点、国际号跳过、无可用账号 → 503。
面板侧覆盖：缺密钥不触上游、空 query / 坏 JSON → 400、成功信封透传、
429 透传、业务码透传、上游不可达 → 502、密钥没绑到可用上游 → 503、每次调用记一条日志。

## 已知边界

- 搜索**不计 Token、不占模型配额**，密钥的每日 Token 限额管不到它；
  要限制搜索频率用 IP 规则或密钥频控。
- 结果里的链接由上游给，面板不校验也不改写；需要过滤时在下游插件里做。
- 上游国际版暂无公开 agenttool 域，国际号参与不了搜索。
