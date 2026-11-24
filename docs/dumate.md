# DuMate 接入（百度搭子）

token-farm 的 **DuMate** 平台对接百度搭子（DuMate）的两套上游：

| 用途 | 地址 |
| --- | --- |
| 网页服务（登录 / token / 签到 / 任务 / 邀请码） | `https://www.dumate.cn/api/dumate` |
| 模型网关（OpenAI 兼容） | `https://dumate-svc.baidu.com/gateway/apis/v1` |

协议照 [`dumate2api`](https://github.com/wearetheone777/dumate2api)（MIT）重写为
**原生实现**，代码在 `server/platforms/dumate/`；任务 / 抽奖 / 邀请码的细节
直接核对网页客户端 bundle，记录在 [`deploy/dumate-ref.txt`](../deploy/dumate-ref.txt)。
不需要额外部署进程、容器或端口，也没有新增 Python 依赖。

```
client ──dumate/<model>──> token-farm 网关
                             │ 鉴权 / 限流 / 记账（与其他平台共用）
                             └──> DumateAdapter
                                    ├── 账号池（data/platforms/dumate/accounts/）
                                    ├── 百度 Passport 扫码 / 手工粘贴 Cookie / 401 重探
                                    └── 模型网关（SSE 原样透传）
```

DuMate 是**单区域**平台（百度搭子国内站），账号与模型一律记 `realm='cn'`。

---

## 1. 数据存放

| 路径 | 内容 |
| --- | --- |
| `<WB_DATA_DIR>/platforms/dumate/accounts/<uid>.json` | 每个账号一个 JSON（**含明文 Cookie**） |

账号 id 取 `dm_<sha256 前缀>`，由 Cookie 派生：同一份凭据重复导入会覆盖同一条
账号。凭据是**网页 Cookie**，必须含 `BDUSS`；其余字段（`BAIDUID` / `STOKEN` /
`PTOKEN` 等）按 `; ` 归一后原样保存。导入阶段**不联网**。

Cookie 拿到模型网关 token 后才算可用：

```
GET /api/dumate/app/token/get  →  {result: {token, expired_at}}
```

token 约 1 小时，进程内缓存并在提前 5 分钟刷新；解析失败按 50 分钟兜底。
没有 refresh token，Cookie 失效只能重新登录（扫码或粘贴）。

---

## 2. 登录

- **扫码**：服务端走百度 Passport 取码 + 长轮询；连不上时退化成「打开登录页 +
  粘回调」。
- **手工导入**：直接粘贴整段 Cookie（或 `Cookie:` 头）。

登录成功后凭据与设备信息一起落盘；探活失败会带「待验证 + 原因」导入，不会把
"能登录"误判成"已失效"。这些与其他网页登录平台共用
[`docs/multi-platform.md`](multi-platform.md) §3 的规则。

---

## 3. 积分 / 签到

- **积分**：`GET /points/quota_overview` 的 `left`，默认 60 秒缓存。
- **签到**：`loginBonusInfo` 查是否已签，未签再 `loginBonus` 领奖；重复调用
  幂等。`earned_credit` 用签到前后 `quota_overview.left` 的差额计算。
- **用量弹窗**：余额 + 套餐明细（`quota_overview.packages`，过期时间上游给
  毫秒，对外统一成 epoch 秒）。

---

## 4. 平台任务

DuMate 账号页的「任务」分段（`supports_tasks=true`）包含成长任务、抽奖与
邀请码。与 `checkin` 的分工：`checkin` 是每日固定动作，调度器会自己跑；
任务由用户**显式**触发。

| 动作 | 上游 | 说明 |
| --- | --- | --- |
| `checkin` | `loginBonusInfo` + `loginBonus` | 签到后刷新任务面板 |
| `complete_task` | `POST /activity/growth-plan/task/complete` | 传 `task_id` 只做一条；不传时只跑可自动任务 |
| `draw` | `POST /activity/growth-plan/draw` | 后端每次生成新 UUID；没有次数时直接 409 |
| `claim_prize` | `POST /activity/growth-plan/prize/claim` | 只给实物奖，积分 / 会员奖自动到账 |
| `bind_invite` | `POST /invitation_code/redeem` | 填写别人的邀请码，一次性 |

### 4.1 成长任务

任务列表 `GET /activity/growth-plan/tasks?device=web`，字段见
`deploy/dumate-ref.txt`。完成判据与网页客户端一致：
`completed_count >= repeat_count`。

可自动完成的类型：

| 类型 | 处理 |
| --- | --- |
| `QUERY_INPUT` | 先用该账号发一条匹配 `query` 的消息（`model-text`，`max_tokens=64`），再上报完成 |
| `USE_SKILL` | 直接上报完成 |
| 其它（`PC_PUSH` / `INVITATION` / `INVITED` / `SUITE` …） | 面板只展示「需在客户端完成」，不代跑 |

上游业务码 `410121` 表示「该任务次数已发放」，并发 / 重复提交时常见，按已
完成处理，不算失败。任务**非每日重置**。

### 4.2 抽奖

`GET /activity/growth-plan/draw/status?source=web` 返回剩余次数、奖品池、
我的奖品与中奖记录；`POST /activity/growth-plan/draw` 的幂等键在 body
（网页客户端用 `x-bce-request-id`，`request_id` 也被接受，本仓两个都带）。
**每次必须新 UUID**，复用会被上游当成重复请求。

成功响应顶层就是中奖记录（`draw_record_id` / `prize_name` / `prize_type` /
`remaining_draws`）。`prize_type=PHYSICAL` 的实物奖需要收货信息后再调
`prize/claim`；对已 SUCCESS 的记录重复领取会报「参数错误:DrawRecordID」。

### 4.3 邀请码

| 接口 | 用途 |
| --- | --- |
| `GET /invitation_code/get?activityId=` | 自己名下的邀请码与生成时间 |
| `GET /invitation_code/invited/count?activityId=` | 已邀请人数 / 累计积分 |
| `POST /invitation_code/redeem` | 填写别人的邀请码 |

规则：邀请码**区分大小写**（只 `strip()`，不折叠）；新账号注册 7 天内可填
一次，不能填自己的码。活动 id 为 `growth_plan_2026`，官方文案「双方各领
2000 积分」。

### 4.4 状态读取与缓存

`task_status` 并发读四路（任务 / 抽奖 / 邀请码 / 邀请统计），**只读**，
不会顺手签到或抽奖。单路失败按路回落账号文件里的 `taskCache`，并把
`{endpoint, code, message}` 放进 `errors`，面板其余分段照常显示。账号页的
批量签到写在账号的 `checkin` / `points` 字段上，任务面板会读取它显示上次
签到结果（不必在任务页再签一次）。

写操作按账号串行，使用独立的 `_task_locks`：完成任务途中 `_token_for` 会拿
token 锁，同一把锁不可重入会死锁。所有写接口由路由记审计
（`run_platform_task`），且要求管理员。

---

## 5. 模型

- 对外模型 id 统一加 `dumate/` 前缀（如 `dumate/model-text`）。
- 上游只认 `max_tokens`：`max_completion_tokens` 会先收敛成单一 `max_tokens`
  （两值不等取较大者），再按模型实测上限钳制。
- 思考档位只对同一后端的白名单模型生效，见 `server/platforms/dumate/reasoning.py`。

---

## 6. 测试

`server/tests/test_platform_dumate_adapter.py` 全部离线（假客户端），覆盖
Cookie 归一与脱敏、目录降级、选号、token 缓存与 401 重试、`max_tokens`
钳制、SSE 扫描、签到与积分、成长任务（字段归一 / 自动完成 / 410121 /
一键完成的跳过口径）、抽奖（请求体双幂等键、每次新 UUID、空池）、邀请码
（拒绝自己的码 / 大小写只 strip）、任务缓存的按路回落，以及百度扫码登录
的事件 / 兑换解析与会话状态机。