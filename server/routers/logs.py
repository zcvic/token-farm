"""请求日志查询。"""
from __future__ import annotations

import csv
import io
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Response

from .. import db, keysvc, security
from ..services import accountlog

router = APIRouter(prefix='/api/logs', tags=['logs'])

# 组合日志导出一次最多给多少行。导出没有分页，上限是**响应体与内存**的护栏：
# 7 天的高频组合可能有几十万行，一次全塞进内存既拖慢面板也拖慢浏览器。
# 超出时只给最近的一段，并用响应头如实告知（前端提示「缩小时间范围」）。
COMBO_EXPORT_MAX_ROWS = 20_000

# 导出列：顺序固定，表头中文（服务端文案不翻译，与界面其它导出同一口径）。
# 「生成耗时 / 输出速率」按统计页同一公式算（latency − first_token），排查
# 「组合为什么比直连慢」时不用再自己拿计算器算一遍。
_COMBO_EXPORT_HEADER = [
    '时间', '实际成员', '版本', '状态', '流式',
    '输入tokens', '输出tokens', '总tokens',
    '缓存命中tokens', '缓存未命中tokens',
    '首字延迟ms', '总耗时ms', '生成耗时ms', '输出速率tok/s',
    '积分', '费用(元)', '密钥', '账号', 'IP', '请求ID', '错误',
]


def _combo_model(name: object) -> str:
    """组合入口名（`combo/<name>`）：查询与导出文件名共用同一份拼法。"""
    return f'combo/{str(name or "").strip()}'


def _combo_where(name: object, days: int | None) -> tuple[str, list[object]]:
    """组合日志的 WHERE 子句（入口名等值 + 可选时间范围）。

    日志页的 `model` 筛选用的是 LIKE（`combo/fast` 会连带 `combo/fast2`），
    而组合卡片要看的永远是「这一个组合」，所以这里必须是等值查询。抽成函数
    是为了 JSON 列表与 CSV 导出不会各写一份、慢慢漂移。
    """
    where = ['l.model = ?']
    args: list[object] = [_combo_model(name)]
    if days:
        # 与日志页同一口径：钳到 [1, 3650]，0 / 空 = 不限时间。
        d = min(3650, max(1, int(days)))
        where.append('l.ts >= ?')
        args.append(int(time.time()) - d * 86400)
    return 'WHERE ' + ' AND '.join(where), args


def _csv_cell(value: object) -> object:
    """挡住 CSV 公式注入（Excel / WPS 会把 `= + - @` 开头的单元格当公式执行）。

    日志里的模型名与错误文案都来自上游响应，属于不可信输入：一条
    `=HYPERLINK("http://…")` 的错误信息就能在用户打开表格时变成可点击的
    外链。数字列不受影响（都是 int / float，不是字符串）。
    """
    if not isinstance(value, str):
        return value
    if value[:1] in ('=', '+', '-', '@'):
        return "'" + value
    return value


def _combo_export_filename(name: object) -> str:
    """下载文件名：只保留 ASCII 安全字符，顺带挡住响应头注入。

    组合名本身的合法字符集是 `[a-z0-9._-]`，但路由参数来自 URL，不能假设
    调用方一定按规矩传——引号 / 换行进 `Content-Disposition` 就是响应头注入。
    """
    safe = re.sub(r'[^A-Za-z0-9._-]+', '-', str(name or '').strip())[:64]
    return f"combo-{safe.strip('-') or 'logs'}-logs-{time.strftime('%Y%m%d-%H%M')}.csv"


@router.get('/account-backfill')
def account_backfill(user: dict = Depends(security.require_admin)) -> dict:
    """账号列的来源自诊断（issue #232）。

    请求日志的「账号」是从**上游运行日志**回填的，面板转发时并不知道用了哪个号。
    这一列空的时候有四种完全不同的原因（模式配错 / 日志路径错 / 上游改了行格式 /
    时间对不上），靠猜只能来回要现场信息。这里把事实直接给出来。
    """
    return accountlog.status()


def _log_item(r) -> dict:
    """把一行 `request_logs` 映射成接口的 item 形状。

    日志页与组合卡片共用：两边要展示的字段一致，分成两份映射迟早会漂移
    （例如新加一列只改了一边）。
    """
    return {
        'id': r['id'],
        'ts': r['ts'],
        'key_id': r['key_id'],
        'key_name': r['key_name'],
        'ip': r['ip'],
        'model': r['model'],
        'mapped_model': r['mapped_model'],
        'status': r['status'],
        'realm': r['realm'] or 'cn',
        # 平台归属（'' = WorkBuddy）。日志页按当前平台过滤后，这里仍如实
        # 返回，便于排查「为什么这条不在我选的平台里」。
        'platform': r['platform'] or '',
        'prompt_tokens': r['prompt_tokens'],
        'completion_tokens': r['completion_tokens'],
        'latency_ms': r['latency_ms'],
        # 首字延迟：NULL 表示未采集（非流式请求或旧记录）
        'first_token_ms': r['first_token_ms'],
        # 首帧延迟：上游第一个带负载的 `data:` 帧到达时刻（issue #493）。
        # 与首字不同口径（不区分 role/推理/正文），所以单独给读端一列；
        # 同样是 NULL = 未采集（非流式、纯保活+[DONE] 的空流、旧记录）。
        'first_frame_ms': (r['first_frame_ms']
                           if 'first_frame_ms' in r.keys() else None),
        # 提示词缓存三段（issue #69）：NULL = 上游没给这三个字段（老上游），
        # 与「给了 0」不同 —— 界面据此显示「—」而不是 0%。
        'cache_hit_tokens': r['cache_hit_tokens'],
        'cache_miss_tokens': r['cache_miss_tokens'],
        'cache_write_tokens': r['cache_write_tokens'],
        'ua': r['ua'],
        'error': r['error'],
        'stream': bool(r['stream']),
        'credit': r['credit'],
        # 本次实际用了哪个上游账号（issue #69）。NULL = 尚未回填 ——
        # 账号是采集上游日志后按时间对回来的，比请求本身晚几秒，
        # 所以刚打完的请求这一列可能是空的（界面显示「—」，刷新即可）。
        'account': r['account'],
        # 思考强度（reasoning_effort）：客户端显式传的档位；NULL=没传/旧记录。
        'effort': r['effort'],
        # 客户端渠道（issue #233）：UA 归类；NULL=该列上线前的旧记录。
        'channel': r['channel'],
        # 响应头 `X-Request-Id` 的值（同步自 workbuddy2api-panel）。
        # NULL = 该功能上线前的记录，或后台任务产生的日志。
        'request_id': r['request_id'] if 'request_id' in r.keys() else None,
        # 按 DeepSeek 官方价估算的费用（元，issue #380）。
        # NULL = 这个模型没收录价目（glm / kimi / minimax …）或该行在功能上线前
        # 写入 —— 界面显示「—」。绝不能兜成 0：那会被读成「这次调用免费」。
        'cost': r['cost'],
    }


@router.get('')
def list_logs(
    page: int = 1,
    size: int = 20,
    key_id: str | None = None,
    model: str | None = None,
    status: str | None = None,
    ip: str | None = None,
    request_id: str | None = None,
    days: int | None = None,
    realm: str | None = None,
    platform: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    # 分页参数钳制（审计 2026-10-18）：`page` 无上限时，超大整数会在 SQLite
    # 绑定期溢出抛 500；`size` 无上限则可以一次拉走整张表（内存与响应体都不受控）。
    page = max(1, min(int(page), 100_000))
    size = max(1, min(int(size), 500))
    where: list[str] = []
    args: list[object] = []

    # 版本过滤（cn / global）。历史记录该列为 NULL —— 按 cn 归类，
    # 与 db.realm_of_model 的口径一致（无前缀即国内版）。
    if realm in ('cn', 'global'):
        if realm == 'cn':
            where.append('(l.realm = ? OR l.realm IS NULL)')
        else:
            where.append('l.realm = ?')
        args.append(realm)

    # 平台过滤：'workbuddy' 表示本家流量（历史行 platform 为 NULL），
    # 其他值按平台名精确匹配；缺省或 'all' 不过滤。
    p = str(platform or '').strip().lower()
    if p and p != 'all':
        if p == 'workbuddy':
            where.append("(l.platform IS NULL OR l.platform = '')")
        else:
            where.append('l.platform = ?')
            args.append(p)

    if days:
        # 钳到 [1, 3650]：超大值会在 SQLite 绑定时溢出（曾 500），
        # 负值则会把过滤条件变成「未来之后」，语义上无意义
        d = min(3650, max(1, int(days)))
        where.append('l.ts >= ?')
        args.append(int(time.time()) - d * 86400)
    if key_id and key_id not in ('all', ''):
        # 非数字会 int() 抛错 → 500。这里显式拦成 400，避免用非法输入探测
        try:
            kid = int(key_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='key_id 必须是数字或 all') from None
        where.append('l.key_id = ?')
        args.append(kid)
    if model:
        where.append('(l.model LIKE ? OR l.mapped_model LIKE ?)')
        args.extend([f'%{model}%', f'%{model}%'])
    if ip:
        where.append('l.ip LIKE ?')
        args.append(f'%{ip}%')
    if request_id:
        # 等值查询：请求 ID 是用户从响应头复制来的完整值，模糊匹配只会
        # 把「同一前缀的其他请求」也带出来，反而干扰定位。
        rid = str(request_id).strip()
        if rid:
            where.append('l.request_id = ?')
            args.append(rid[:128])
    if status == 'ok':
        where.append('l.status >= 200 AND l.status < 300')
    elif status == 'error':
        where.append('(l.status < 200 OR l.status >= 300)')

    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query_one(f'SELECT COUNT(*) AS c FROM request_logs l {clause}', args)['c']

    page = max(1, page)
    size = min(200, max(1, size))
    rows = db.query(
        f'SELECT l.*, k.name AS key_name FROM request_logs l '
        f'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
        f'ORDER BY l.id DESC LIMIT ? OFFSET ?',
        (*args, size, (page - 1) * size),
    )
    return {'total': total, 'items': [_log_item(r) for r in rows]}


@router.get('/combo/{name}')
def combo_logs(
    name: str,
    days: int = 7,
    limit: int = 50,
    user: dict = Depends(security.current_user),
) -> dict:
    """某个组合的请求日志（按 `combo/<name>` **精确匹配**）。

    日志页的 `model` 筛选用的是 LIKE（会连带 `combo/foo2`），而组合卡片要看的
    是「这一个组合实际把请求发给了谁」，所以这里用等值查询。

    汇总（成功率 / 平均耗时 / token）按**整个时间范围**算，不是只算返回的那
    一屏：只算前 N 条的话，刷新或换个数就会变，用户没法用它判断这个组合稳不稳。
    平均耗时只取成功请求——429 / 上游 5xx 往往几毫秒就返回，混进去会把均值
    拉低，看起来比实际更快。
    """
    combination = _combo_model(name)
    limit = max(1, min(int(limit), 200))
    clause, args = _combo_where(name, days)

    stats = db.query_one(
        f'SELECT COUNT(*) AS total, '
        f'COALESCE(SUM(CASE WHEN l.status >= 200 AND l.status < 300 '
        f'THEN 1 ELSE 0 END), 0) AS success, '
        f'COALESCE(SUM(CASE WHEN l.status >= 200 AND l.status < 300 '
        f'THEN l.latency_ms ELSE 0 END), 0) AS ok_latency, '
        f'COALESCE(SUM(l.prompt_tokens + l.completion_tokens), 0) AS tokens, '
        f'COALESCE(SUM(l.credit), 0) AS credit '
        f'FROM request_logs l {clause}',
        args,
    )
    total = int(stats['total'] or 0)
    success = int(stats['success'] or 0)
    rows = db.query(
        f'SELECT l.*, k.name AS key_name FROM request_logs l '
        f'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
        f'ORDER BY l.id DESC LIMIT ?',
        (*args, limit),
    )
    return {
        'model': combination,
        'total': total,
        'items': [_log_item(r) for r in rows],
        'summary': {
            'total': total,
            'success': success,
            'failed': total - success,
            'success_rate': (success / total) if total else None,
            'avg_latency_ms': round(int(stats['ok_latency'] or 0) / success)
                              if success else None,
            'total_tokens': int(stats['tokens'] or 0),
            'total_credit': stats['credit'] or 0,
        },
    }


@router.get('/combo/{name}/export')
def export_combo_logs(
    name: str,
    days: int = 7,
    user: dict = Depends(security.current_user),
) -> Response:
    """把某个组合的请求日志导出成 CSV（Excel / 表格工具可直接打开）。

    与弹窗列表的区别只有两点，都是导出该有的语义：

      · **不受列表条数限制**：弹窗一次只显示 100 条，导出要按整个时间范围给，
        否则「导出」只是把屏幕上的几行复制出去；
      · **带上生成耗时与输出速率**：同一套公式（`latency − first_token`）与
        统计页一致，排查「组合为什么比直连慢」时可以直接按成员 / 时间透视。

    行数仍有上限（`COMBO_EXPORT_MAX_ROWS`），超出时只给**最近**的一段，并用
    响应头 `X-Export-Truncated` / `X-Export-Rows` / `X-Export-Total` 如实告知，
    前端据此提示用户缩小时间范围——不静默截断。
    """
    combination = _combo_model(name)
    clause, args = _combo_where(name, days)
    total = int(db.query_one(
        f'SELECT COUNT(*) AS c FROM request_logs l {clause}', args)['c'])
    # 多取一行只为判断「是否被截断」，返回前再切掉。
    rows = db.query(
        f'SELECT l.*, k.name AS key_name FROM request_logs l '
        f'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
        f'ORDER BY l.id DESC LIMIT ?',
        (*args, COMBO_EXPORT_MAX_ROWS + 1),
    )
    truncated = len(rows) > COMBO_EXPORT_MAX_ROWS
    if truncated:
        rows = rows[:COMBO_EXPORT_MAX_ROWS]
    # 查的时候按 id 倒序取「最近 N 条」，导出时翻回时间正序：CSV 是拿去画表 /
    # 透视的，倒序会让最旧的一行跑在最上面。
    rows = list(reversed(rows))

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(_COMBO_EXPORT_HEADER)
    has_request_id = bool(rows) and 'request_id' in rows[0].keys()
    for r in rows:
        first = int(r['first_token_ms'] or 0)
        latency = int(r['latency_ms'] or 0)
        gen_ms = latency - first if latency > first else None
        completion = int(r['completion_tokens'] or 0)
        tps = (round(completion / (gen_ms / 1000.0), 2)
               if gen_ms and completion > 0 else '')
        # 成员全失败时 `mapped_model` 会回填成组合入口名本身——那不是「实际
        # 成员」，导出里留空，与弹窗显示「—」同一口径。
        member = (r['mapped_model'] or '')
        if member == combination:
            member = ''
        writer.writerow([
            time.strftime('%Y-%m-%d %H:%M:%S',
                          time.localtime(int(r['ts'] or 0))),
            _csv_cell(member),
            r['realm'] or 'cn',
            int(r['status'] or 0),
            '是' if r['stream'] else '否',
            int(r['prompt_tokens'] or 0),
            completion,
            int(r['prompt_tokens'] or 0) + completion,
            int(r['cache_hit_tokens'] or 0),
            int(r['cache_miss_tokens'] or 0),
            first or '',
            latency,
            gen_ms if gen_ms is not None else '',
            tps,
            r['credit'] if r['credit'] is not None else '',
            r['cost'] if r['cost'] is not None else '',
            _csv_cell(r['key_name'] or ''),
            _csv_cell(r['account'] or ''),
            r['ip'] or '',
            (r['request_id'] or '') if has_request_id else '',
            _csv_cell(r['error'] or ''),
        ])

    # 加 BOM：Excel 打开 UTF-8 CSV 才不会把中文变成乱码（与红包导出同一做法）。
    body = ('\ufeff' + buf.getvalue()).encode('utf-8')
    return Response(
        content=body,
        media_type='text/csv; charset=utf-8',
        headers={
            'Content-Disposition':
                f'attachment; filename="{_combo_export_filename(name)}"',
            'X-Export-Rows': str(len(rows)),
            'X-Export-Truncated': '1' if truncated else '0',
            'X-Export-Total': str(total),
        },
    )


@router.post('/clear')
def clear_logs(user: dict = Depends(security.require_session_admin)) -> dict:
    db.execute('DELETE FROM request_logs')
    return {'ok': True}
