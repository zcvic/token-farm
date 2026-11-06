# Prometheus 指标

管理端内置一个 Prometheus 文本端点（格式 0.0.4），不需要额外的 exporter：

```
GET /api/metrics
GET /metrics          # 同一个处理函数的别名
```

## 鉴权

指标里有按模型的调用量、Token 与积分消耗，属于运营数据，**默认要求管理面
身份**。两种方式：

1. 浏览器已登录管理端（会话 Cookie）；
2. `Authorization: Bearer wbt_...` —— 建议在「设置 → API Token」建一个
   `readonly` 令牌，只给 Prometheus 用。

```yaml
# prometheus.yml
scrape_configs:
  - job_name: token-farm
    metrics_path: /metrics
    authorization:
      credentials: wbt_xxxxxxxxxxxxxxxx
    static_configs:
      - targets: ['127.0.0.1:7864']
```

> 7864 不要直接暴露到公网。默认部署应放在反向代理之后；即使指标端点要鉴权，
> 也不该把它变成互联网上的探测面。

## 指标

| 指标 | 类型 | 说明 |
|---|---|---|
| `tokenfarm_build_info{version}` | gauge | 版本信息，恒为 1 |
| `tokenfarm_uptime_seconds` | gauge | 进程运行时长 |
| `tokenfarm_requests_total{status}` | counter | 网关请求，`success` / `failed` |
| `tokenfarm_requests_streaming_total` | counter | 其中流式请求数 |
| `tokenfarm_tokens_total{type}` | counter | `prompt` / `completion` |
| `tokenfarm_prompt_cache_tokens_total{type}` | counter | `hit` / `miss` |
| `tokenfarm_credit_total` | counter | 上游 usage 报告的真实积分 |
| `tokenfarm_model_requests_total{model,status}` | counter | 按模型的请求数 |
| `tokenfarm_model_tokens_total{model,type}` | counter | 按模型的 Token |
| `tokenfarm_model_prompt_cache_tokens_total{model,type}` | counter | 按模型的缓存 Token |
| `tokenfarm_model_credit_total{model}` | counter | 按模型的积分 |
| `tokenfarm_model_streaming_requests_total{model}` | counter | 按模型的流式请求 |
| `tokenfarm_model_avg_ttfb_seconds{model}` | gauge | 平均首字延迟（只统计有观测的流式请求） |
| `tokenfarm_model_avg_latency_seconds{model}` | gauge | 平均总延迟 |
| `tokenfarm_model_tokens_per_second{model}` | gauge | 生成段速率（非流式不计入分母） |
| `tokenfarm_metrics_dropped_models` | gauge | 因标签上限未跟踪的模型数 |

## 口径与限制

- **进程内累计**：从进程启动开始，重启归零。这是 counter 的正常语义，
  Prometheus 会自行处理 reset；也因此不会因为请求日志被清理而「倒退」。
- 指标在 `gateway._record` 打点，chat / Responses / Anthropic / 平台调用共用；
  `/v1/models`、`/v1/usage` 这类 0 token 的管理端点不计入请求数。
- `success` / `failed` 只看 HTTP 状态（2xx 为成功），与日志页筛选口径一致。
  上游 429 计为 failed；要看限流原因请到日志页。
- 默认最多跟踪 512 个模型，避免外部模型目录把内存撑大。超出后新模型不再
  跟踪，可通过 `tokenfarm_metrics_dropped_models` 观察。
