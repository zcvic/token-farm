"""进程内 Prometheus 指标（文本格式 0.0.4），零第三方依赖。

## 为什么是进程内累计

面板已经在 SQLite 里记了每一行请求日志，但 Prometheus 抓取要的是**单调递增
的计数器**。每次抓取都去扫全表既慢，又会让计数器在日志清理时倒退（Prometheus
会把倒退当成 counter reset）。所以这里维护一份自进程启动以来的累计值：

  * 抓取是 O(模型数)，与日志量无关；
  * 不因请求日志保留期清理而跳变；
  * 进程重启归零，Prometheus 自己处理 reset，符合 counter 语义。

## 口径

所有指标都从 `gateway._record` 这一个入口打点 —— 它是 chat / Responses /
Anthropic / 平台调用的**公共记账点**（见该函数注释）。一处漏记的表现会是
「某个协议在 Grafana 里完全没流量」，所以不要把打点散到各协议里。

`status` 只分 success（2xx）/ failed，与日志页的筛选口径一致。上游 429
属于 failed —— 它确实没有完成调用；是否要单独看限流，用日志页的 status 维度。
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

# 最多跟踪多少个模型：模型目录是外部输入（上游 + 平台），不设上限时一个
# 恶意/异常的上游能让我们为每个随机 id 分配一个累加器，内存无界增长。
MAX_MODELS = 512

_started = time.time()
_lock = threading.Lock()


@dataclass
class _Acc:
    requests: int = 0
    success: int = 0
    failed: int = 0
    streaming: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit: int = 0
    cache_miss: int = 0
    credit: float = 0.0
    latency_sum_ms: float = 0.0
    ttfb_sum_ms: float = 0.0
    ttfb_n: int = 0
    # 生成段耗时与 token 必须同一样本：非流式没有 TTFB，不能把它算进
    # 生成速率的分母，否则 tokens/s 会虚高（与目标实现口径一致）。
    gen_sec: float = 0.0
    gen_tokens: int = 0


_by_model: dict[str, _Acc] = {}
_dropped_models: set[str] = set()


def _label(value: object, limit: int = 128) -> str:
    """标签值清洗：Prometheus 文本格式里反斜杠、引号、换行都要转义。

    模型名是外部输入，必须截断 —— 一个 1 MiB 的名字会把每次抓取的响应体
    放大到不可接受，也违反标签基数的常识。
    """
    text = str(value or '-').replace('\n', ' ').replace('\r', ' ')
    return text[:limit] or '-'


def _escape(value: str) -> str:
    return value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _num(value: float) -> str:
    if math.isnan(value):
        return 'NaN'
    if math.isinf(value):
        return '+Inf' if value > 0 else '-Inf'
    if value == int(value):
        return str(int(value))
    return repr(float(value))


def note_request(*, model: object, status: int, stream: bool = False,
                 prompt_tokens: int = 0, completion_tokens: int = 0,
                 first_token_ms: int | None = None, latency_ms: int = 0,
                 cache_hit: int | None = None, cache_miss: int | None = None,
                 credit: float | None = None) -> None:
    """记一次调用。任何异常都不能冒泡到请求路径（指标是旁路）。"""
    try:
        name = _label(model)
        with _lock:
            acc = _by_model.get(name)
            if acc is None:
                if len(_by_model) >= MAX_MODELS:
                    # 只记录「被丢弃过」，不无限增长；抓取里给一个计数，
                    # 让运维知道标签已被截断，而不是以为流量消失。
                    _dropped_models.add(name)
                    return
                acc = _Acc()
                _by_model[name] = acc
            acc.requests += 1
            if 200 <= int(status) < 300:
                acc.success += 1
            else:
                acc.failed += 1
            if stream:
                acc.streaming += 1
            pt = max(0, int(prompt_tokens or 0))
            ct = max(0, int(completion_tokens or 0))
            acc.prompt_tokens += pt
            acc.completion_tokens += ct
            lat = max(0, int(latency_ms or 0))
            acc.latency_sum_ms += lat
            ttfb = first_token_ms
            if ttfb is not None and int(ttfb) > 0:
                ttfb = int(ttfb)
                acc.ttfb_sum_ms += ttfb
                acc.ttfb_n += 1
                gen_ms = max(0, lat - ttfb)
                if ct > 0 and gen_ms > 0:
                    acc.gen_sec += gen_ms / 1000.0
                    acc.gen_tokens += ct
            if cache_hit is not None:
                acc.cache_hit += max(0, int(cache_hit))
            if cache_miss is not None:
                acc.cache_miss += max(0, int(cache_miss))
            if credit is not None:
                try:
                    value = float(credit)
                except (TypeError, ValueError):
                    value = 0.0
                if math.isfinite(value) and value > 0:
                    acc.credit += value
    except Exception:  # noqa: BLE001 — 指标绝不影响业务
        return


def reset() -> None:
    """清空累计（测试用；生产不要调用 —— counter 倒退会触发假 reset）。"""
    with _lock:
        _by_model.clear()
        _dropped_models.clear()


class _Writer:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.seen: set[str] = set()

    def family(self, name: str, help_text: str, kind: str) -> None:
        if name in self.seen:
            return
        self.seen.add(name)
        self.lines.append(f'# HELP {name} {help_text}')
        self.lines.append(f'# TYPE {name} {kind}')

    def sample(self, name: str, value: float, **labels: str) -> None:
        if labels:
            body = ','.join(f'{k}="{_escape(_label(v))}"' for k, v in labels.items())
            self.lines.append(f'{name}{{{body}}} {_num(value)}')
        else:
            self.lines.append(f'{name} {_num(value)}')


def render(version: str = '') -> str:
    """渲染 Prometheus 文本格式。"""
    with _lock:
        snapshot = {name: _Acc(**vars(acc)) for name, acc in _by_model.items()}
        dropped = len(_dropped_models)

    total = _Acc()
    for acc in snapshot.values():
        total.requests += acc.requests
        total.success += acc.success
        total.failed += acc.failed
        total.streaming += acc.streaming
        total.prompt_tokens += acc.prompt_tokens
        total.completion_tokens += acc.completion_tokens
        total.cache_hit += acc.cache_hit
        total.cache_miss += acc.cache_miss
        total.credit += acc.credit
        total.latency_sum_ms += acc.latency_sum_ms
        total.ttfb_sum_ms += acc.ttfb_sum_ms
        total.ttfb_n += acc.ttfb_n
        total.gen_sec += acc.gen_sec
        total.gen_tokens += acc.gen_tokens

    w = _Writer()
    w.family('tokenfarm_build_info', 'Build information.', 'gauge')
    w.sample('tokenfarm_build_info', 1, version=_label(version or 'unknown'))
    w.family('tokenfarm_uptime_seconds', 'Seconds since this process started.', 'gauge')
    w.sample('tokenfarm_uptime_seconds', max(0.0, time.time() - _started))

    w.family('tokenfarm_requests_total', 'Gateway requests by result.', 'counter')
    w.sample('tokenfarm_requests_total', total.success, status='success')
    w.sample('tokenfarm_requests_total', total.failed, status='failed')
    w.family('tokenfarm_requests_streaming_total',
             'Gateway requests served as SSE streams.', 'counter')
    w.sample('tokenfarm_requests_streaming_total', total.streaming)

    w.family('tokenfarm_tokens_total', 'Tokens observed at the gateway.', 'counter')
    w.sample('tokenfarm_tokens_total', total.prompt_tokens, type='prompt')
    w.sample('tokenfarm_tokens_total', total.completion_tokens, type='completion')
    w.family('tokenfarm_prompt_cache_tokens_total',
             'Upstream prompt cache tokens. Hit rate = hit/(hit+miss).', 'counter')
    w.sample('tokenfarm_prompt_cache_tokens_total', total.cache_hit, type='hit')
    w.sample('tokenfarm_prompt_cache_tokens_total', total.cache_miss, type='miss')
    w.family('tokenfarm_credit_total', 'Upstream credit reported by usage.', 'counter')
    w.sample('tokenfarm_credit_total', total.credit)

    w.family('tokenfarm_model_requests_total', 'Gateway requests by model.', 'counter')
    for name in sorted(snapshot):
        acc = snapshot[name]
        w.sample('tokenfarm_model_requests_total', acc.success, model=name, status='success')
        w.sample('tokenfarm_model_requests_total', acc.failed, model=name, status='failed')
    w.family('tokenfarm_model_tokens_total', 'Tokens by model.', 'counter')
    for name in sorted(snapshot):
        acc = snapshot[name]
        w.sample('tokenfarm_model_tokens_total', acc.prompt_tokens, model=name, type='prompt')
        w.sample('tokenfarm_model_tokens_total', acc.completion_tokens, model=name, type='completion')
    w.family('tokenfarm_model_prompt_cache_tokens_total',
             'Upstream prompt cache tokens by model.', 'counter')
    for name in sorted(snapshot):
        acc = snapshot[name]
        w.sample('tokenfarm_model_prompt_cache_tokens_total', acc.cache_hit,
                 model=name, type='hit')
        w.sample('tokenfarm_model_prompt_cache_tokens_total', acc.cache_miss,
                 model=name, type='miss')
    w.family('tokenfarm_model_credit_total', 'Observed upstream credit by model.', 'counter')
    for name in sorted(snapshot):
        w.sample('tokenfarm_model_credit_total', snapshot[name].credit, model=name)
    w.family('tokenfarm_model_streaming_requests_total',
             'Streaming requests by model.', 'counter')
    for name in sorted(snapshot):
        w.sample('tokenfarm_model_streaming_requests_total',
                 snapshot[name].streaming, model=name)
    w.family('tokenfarm_model_avg_ttfb_seconds', 'Average TTFB by model (streams only).', 'gauge')
    for name in sorted(snapshot):
        acc = snapshot[name]
        avg = (acc.ttfb_sum_ms / acc.ttfb_n / 1000.0) if acc.ttfb_n else 0.0
        w.sample('tokenfarm_model_avg_ttfb_seconds', avg, model=name)
    w.family('tokenfarm_model_avg_latency_seconds', 'Average request latency by model.', 'gauge')
    for name in sorted(snapshot):
        acc = snapshot[name]
        avg = (acc.latency_sum_ms / acc.requests / 1000.0) if acc.requests else 0.0
        w.sample('tokenfarm_model_avg_latency_seconds', avg, model=name)
    w.family('tokenfarm_model_tokens_per_second',
             'Completion tokens per second during generation.', 'gauge')
    for name in sorted(snapshot):
        acc = snapshot[name]
        rate = (acc.gen_tokens / acc.gen_sec) if acc.gen_sec > 0 else 0.0
        w.sample('tokenfarm_model_tokens_per_second', rate, model=name)

    w.family('tokenfarm_metrics_dropped_models',
             'Models omitted because the label-cardinality cap was reached.', 'gauge')
    w.sample('tokenfarm_metrics_dropped_models', dropped)
    return '\n'.join(w.lines) + '\n'
