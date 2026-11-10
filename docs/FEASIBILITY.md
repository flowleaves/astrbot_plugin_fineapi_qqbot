# fineapi-qqbot 可行性报告（MVP）

> 目标：为星芽（`xingya.site`）做一个 AstrBot QQ 机器人插件，提供
> `/绑定 <用户名> <ID>`、`/余额`、`/模型状态`，并在首次绑定时赠送
> 1 美元额度（后台可调）。`/签到` 本期暂缓。
>
> 参考项目：`cankao/astrbot_plugin_newapi_helper`（Shawlei，v0.0.37）
> 平台源码：`xingya-newapi-new`（当前 `VERSION` = `xingya-rc50`）
> 编写日期：2026-11-10
>
> **已确认的产品决策（2026-11-10）**
>
> 1. 绑定格式：`/绑定 <用户名> <ID>`（用户名在前、ID 为纯数字），两者都匹配才绑定成功；
> 2. 签到：**暂不做**；
> 3. 首绑礼：发到**普通余额**（`add_quota`）；
> 4. 防薅：**取消**。机器人使用 QQ 官方机器人，拿不到 QQ 等级/入群时长，
>    按产品决定不做门槛。
>
> 因此本报告的 MVP 范围已收敛为：**绑定 / 余额 / 首绑礼 / 帮助**，
> 模型状态排在其后；签到相关章节仅作后续参考。
>
> 当前进度：MVP + 模型状态 + 芽点抽奖已落地，版本 `0.3.2`；仓库根目录即插件目录。

---

## 0. 结论（先看这里）

**整体可行。绑定格式、签到范围、首绑礼去向已定；剩余只需确认防薅阈值和适配器。**

| 能力 | 结论 | 是否需要改 NewAPI | 主要风险 |
|---|---|---|---|
| `/绑定 <用户名> <ID>` | ✅ 已实现 | 否 | ID + 用户名只是轻校验，不等于账号归属证明 |
| `/余额` | ✅ 可做 | 否 | 群聊泄露余额、管理 API 频率 |
| 首绑送 $1 | ✅ 已实现 | 否 | 幂等（重试重复发放）；官方机器人无法做等级/IP 风控，已取消门槛 |
| 芽点抽奖 | ✅ 已实现 | 否 | 重复开奖、超发、主动消息失败 |
| `/签到` | ⏸ 暂不做 | 需要时再评估 | 星芽签到被限制为“浏览器会话 + Turnstile”，机器人无法直接领 |
| `/模型状态` | ✅ 已实现 | 否 | 日志量/4xx 误报；已用“排除 4xx + 用户去重”处理 |

建议落地路径：

1. **MVP v0.1**：绑定（ID + 用户名）+ 余额 + 首绑礼 + 帮助，纯 NewAPI 管理 API，零服务端改动。
2. **v0.2**：模型状态（已实现）+ 芽点抽奖（已实现）+ 运营功能（排行榜、余额预警）。
3. **以后（可选）**：签到，二选一：服务端 `/api/bot/*` 桥接复用官方签到服务，或只回签到页链接。

> 一句话：**绑定/余额/送礼现在就能做；签到不要仿照参考项目直写数据库，星芽的签到账务是独立钱包 + 批次过期体系，直写会破坏账。**

---

## 1. 参考项目解剖

### 1.1 它是什么

`astrbot_plugin_newapi_helper` 是一个 28+ 命令的“群内 NewAPI 运营插件”：

- 账号：`/注册`、`/绑定 <ID>`、`/密码绑定`、`/解绑`、`/找回密码`
- 用量：`/余额`、`/签到`、`/排行榜`
- 玩法：红包、抢劫、转账、猜大小、猜点数、斗地主、象棋、五子棋、模拟股市
- 管理：`/查用户`、`/强制解绑`、退群自动解绑/删号
- 两种模式：**MySQL 直连（推荐）** / **管理员 API 回退**

### 1.2 技术事实

| 项 | 值 |
|---|---|
| 代码规模 | `main.py` 单文件约 167 KB / 3500+ 行 |
| 依赖 | `aiohttp`、`aiomysql`、`bcrypt`、`argon2-cffi` |
| 存储 | 插件 data 目录下 JSON 文件（`bindings.json` 等） |
| 数据库 | 直连 MySQL，`users` / `checkins` 表读写 |
| 绑定流程 | 群里发 `/绑定 <ID>` → 私聊发送“账号 密码”验证 → 绑定成功后换分组 |
| 签到 | 数据库模式直接更新 `users.quota` + 写 `checkins`；API 模式登录站点调签到接口 |
| 权限 | AstrBot 管理员 + 插件配置 `admin_qqs` |
| 配置 | `_conf_schema.json`，含 `db.*` 嵌套配置 |

### 1.3 值得借鉴

- `/绑定 <ID>` 的两步式交互（群里发起、私聊验证）体验好。
- 管理员账号保护（`role >= 10` 不允许被绑定）。
- 一个 NewAPI 用户 ID 只能被一个 QQ 绑定（`find_by_user_id`）。
- 群白名单、私聊敏感信息、临时会话发送密码。
- 绑定/签到状态持久化，重启不丢。
- 命令别名、自定义前缀。

### 1.4 不建议照搬

| 问题 | 说明 | 我们的做法 |
|---|---|---|
| 单文件 167 KB | 难维护、难测试 | 拆分为 `main.py` + `core/` + `commands/` |
| 依赖 MySQL 直连 | 星芽是 **PostgreSQL**；直写 `users.quota` 绕过审计/钱包 | 只用管理 API；不直连业务库 |
| 直接写签到表 | 星芽签到是独立钱包 + 30 天批次 + 轮次，写 `checkins` 已失效 | 走官方签到服务或只发链接 |
| 保存用户密码 | 即使混淆也存在泄露面 | **不保存任何密码**，绑定不用密码 |
| 同步 JSON 存储 | 并发下可能丢写 | SQLite（WAL）+ 事务 |
| 功能过载 | 游戏/股市/红包带来合规与维护成本 | MVP 只做 4 个命令 |
| 无测试 | 回归靠手测 | 至少覆盖绑定校验、送礼幂等、异常状态处理 |
| 假设 MySQL 表结构 | 与 rc50 的表/字段不一致 | 通过 API 取数，不耦合表结构 |

---

## 2. 目标平台差异（决定方案的关键事实）

### 2.1 币值口径

- 内部额度单位：`quota`，`QuotaPerUnit = 500000`（1 USD = 500000 quota）。
- 星芽展示单位「芽点」：**100 芽点 = 1 USD = 500000 quota**，即 **1 芽点 = 5000 quota**。
- 因此首绑礼 `$1` = **100 芽点 = 500000 quota**。
- 文案建议同时显示：`$1.00（100 芽点）`，避免用户误解。

### 2.2 认证与权限

- 管理接口：`Authorization: Bearer <个人访问令牌 PAT>`。rc50 以令牌归属识别用户，**不再依赖** `New-Api-User` 头（旧版/旧网关才需要，可作为兼容配置保留）。
- rc50 的 PAT 是**带 scope** 的，最小权限即可，不需要 root 密码：
  - 查用户：`user:read`
  - 调额度（首绑礼）：`user:write`
  - 模型状态（错误日志）：`log:read`
  - 渠道状态/价格表（降级）：`channel:read` / `GET /api/pricing`
- 注意 rc50 的 scope 规则是“未声明路由一律拒绝”（fail closed），因此只能调用已声明的管理路由；上面几条均已声明。
- 普通用户登录接口受 `TurnstileCheck` 保护，且支持密码加密传输、2FA、Passkey，**不适合机器人代登录**。
- 用户签到接口额外要求 **浏览器会话**：
  - `POST /api/user/xingya-daily-claim` 在 PAT 路由表中被标为 **session-only**，带 PAT 直接返回 401 / `checkin_session_required`；
  - 同时有 Turnstile 与用户级限流。
  - 旧 `POST /api/user/checkin` 已返回 410。
  - `GET /api/user/xingya-checkin` 虽然允许 `wallet:read`，但只返回**令牌所属者**的数据，没有“管理员查任意用户签到状态”的等价接口。

### 2.3 签到体系（星芽自研）

- 独立签到钱包：`xingya_checkin_credit_lots` / `xingya_checkin_allocations`，奖励按批次 **30 天过期**；
- 每日奖励区间、固定档位、目标分组、领取开关都由后台配置；
- 有 `claim_round`（当天可后台刷新再领一轮）；
- 签到额度只用于签到专用分组，不参与普通余额补扣/邀请资格。

> 这意味着参考项目“直接 `UPDATE users SET quota = quota + X`”的签到方式在星芽会：
> ① 绕过过期与钱包约束；② 与站内签到重复领取；③ 破坏审计与对账；④ 让签到分组资金口径失真。
> **不能这么做。**

### 2.4 部署链路

```
用户 → Cloudflare → Nginx → NewAPI(rc50) → (PostgreSQL + Redis) → AxonHub → 上游
```

机器人访问 `https://xingya.site/api/*` 时会经过 Cloudflare，需要关注：

- WAF / Bot 防护是否拦截服务器出口 IP；
- `/api/*` 是否被缓存或限流；
- 建议给机器人固定出口 IP 加白名单，或走源站直连（不经 CF）。

---

## 3. 需求可行性拆解

### 3.1 `/绑定 <用户名> <ID>` —— 已实现

数据来源：`GET /api/user/:id`（`AdminAuth` + `user:read`）；按用户名/关键词查找可用 `GET /api/user/search?keyword=...`（同为 `user:read`）。

返回可用字段（示意）：

```json
{
  "success": true,
  "data": {
    "id": 123,
    "username": "alice",
    "display_name": "Alice",
    "role": 1,
    "status": 1,
    "quota": 500000,
    "used_quota": 123456,
    "request_count": 42,
    "group": "default",
    "created_at": 1730000000
  }
}
```

**校验规则（v0.1）**：

1. 格式为 `/绑定 <用户名> <ID>`：最后一个参数必须是纯数字 ID，前面的全部文本作为用户名（允许空格），去掉首尾空格；
2. 调 `GET /api/user/:id`，要求 `data.username` 与输入**严格相等**（大小写敏感，与站内登录一致）；
   - 同时兼容旧的“ID 在前”写法，但成功后会提示新格式；
3. ID 与用户名任一不匹配：统一回复“ID 与用户名不匹配或账号不存在”，不区分具体原因，降低枚举价值；
4. `role >= 10`（管理员/超管）禁止绑定；
5. `status != 1`（封禁/禁用）禁止绑定；
6. 一个星芽用户只能被一个平台用户绑定；一个平台用户只能绑定一个星芽账号；
7. 绑定失败不落库、不发礼、不泄露账号其他信息（邮箱、余额等）。

**限流（本版本）**：

- 只对 `/余额` 做每用户 3 秒冷却，避免刷管理 API；
- 绑定不做失败锁定（官方机器人拿不到足够信号，且产品已取消风控门槛）；
- 如需防探测，可在站点侧或 AstrBot 侧加限流。

**安全性定位（必须说明）**：

`ID + 用户名` 属于 **L0.5 轻校验**，不是账号归属证明——用户名可能是公开信息，攻击者仍可能抢绑他人 ID。
它的价值是：防手滑误绑、提高批量尝试成本、减少“随便填个 ID”的骚扰，而不是防定向冒用。

| 级别 | 方式 | 安全性 | 体验 | 是否改服务端 |
|---|---|---|---|---|
| L0 | 只输 ID | 低 | 最好 | 否 |
| **L0.5** | **ID + 用户名**（本方案） | **低中** | **好** | **否** |
| L1 | ID + 只读 PAT（`profile:read`），校验后立即丢弃 | 中高 | 中 | 否 |
| L2 | 站点生成 6 位一次性绑定码 | 高 | 好 | 是（小改动） |

约束：

- 在**没有任何“可转移额度”功能**（转账/红包/兑换）之前，L0.5 可以接受；
- 一旦要上线这类功能，必须升级到 L1/L2；
- 管理员可用 `/强制解绑` 处理抢绑；被抢绑的号主申诉后由管理员解绑再重新绑定。
- **官方机器人注意**：QQ 官方机器人给的是 OpenID，同一自然人在不同群/私聊可能得到
  不同 OpenID，因此“一个 QQ 只能绑一个账号”实际是“一个平台用户 ID 只能绑一个账号”。
  若要做跨群唯一，需要额外身份（手机号/邮箱/绑定码），本期不做。

首绑礼不做额外风控门槛（官方机器人拿不到等级/IP，产品决定取消）；
只保留“一人一次/一 QQ 一次”的幂等约束，详见 §3.3。

### 3.2 `/余额` —— 可行

同样用 `GET /api/user/:id`，展示：

```
👤 alice（ID 123）
💰 余额：$1.2345（123.45 芽点）
📊 累计消耗：$0.4567（45.67 芽点）
🔢 调用次数：42
```

注意：

- 群聊默认只显示脱敏/简版，或要求私聊（可配置）；
- 结果缓存 10–30 秒，避免刷屏触发管理 API 限流；
- 不返回邮箱、IP、密钥等敏感字段。

### 3.3 首绑送 $1 —— 已实现

接口：`POST /api/user/manage`（`AdminAuth` + `user:write`）

```json
{ "id": 123, "action": "add_quota", "mode": "add", "value": 500000 }
```

- `value` 必须用整数 quota；`$1 = 500000`。
- **已确认：发到普通余额**，不是签到钱包；立即可用、不过期。
- 会写入 `LogTypeTopup` 与操作审计日志（操作者 = 机器人用的管理员账号）。
- 不能给同级或更高角色加额度；目标不存在/超额会报错。

**老用户过滤（2026-11-10 新增）**：

- 配置 `gift.new_user_id_after`，默认 `8352`；
- 只有 `newapi_user_id > 阈值` 的新用户才发放；
- `newapi_user_id <= 阈值` 视为老用户，`/绑定` 和 `/领取首绑礼` 都不发放，
  本地标记 `gift_state='skipped'`，且**不向用户显示首绑礼提示**；
- 管理员 `/补发首绑礼 <平台用户ID> force` 可人工覆盖；
- 阈值填 `0` 表示不限制。

**幂等设计（最容易翻车的点）**：

```
bindings(qq primary key, newapi_user_id unique, gift_state, gift_quota, gift_at, gift_reason)
```

流程（已实现）：

1. 绑定成功 → 写入 `gift_state='pending'`；
2. 调 `add_quota`；
3. 成功 → `gift_state='granted'`，记录 `gift_at`；
4. 明确 API 失败 → `gift_state='failed'`，用户可 `/领取首绑礼` 重试；
5. 请求超时/连接失败（没有有效 HTTP 响应）→ `gift_state='unknown'`，
   **不自动重试**，避免重复发放；管理员核对后 `/补发首绑礼 <平台用户ID> force`；
6. 启动时把超过 10 分钟仍为 `pending` 的记录标为 `unknown`。

**风控：已取消**：

- 机器人使用 QQ 官方机器人，无法获取 QQ 等级、入群时长或 QQ 用户 IP；
- 星芽 rc50 也不存注册 IP（`model.User` 无该字段），因此 IP 方案同样不可行；
- 产品决定不做门槛，只保留：
  - 一个星芽用户只能被一个平台用户绑定；
  - 一个平台用户只能绑定一个星芽账号；
  - 礼物每个绑定关系只发一次；
  - 管理员可 `/补发首绑礼` 处理失败/未知状态。
- 接受“批量小号”风险；如需控制成本，建议用**后台礼物金额/活动预算**，而不是机器人侧门槛。

- 管理员补发命令 `/补发首绑礼 <平台用户ID> [force]`；
- 后台可调：`gift.amount_usd`（默认 1.0），换算 `round(usd * quota_per_unit)`。

### 3.4 `/签到` —— 暂不做（本节仅保留后续参考）

> 2026-11-10 决策：MVP 不做签到。下面内容是以后要重启该功能时的评估，
> 不进入当前开发范围。

先明确：**星芽现有签到不能由机器人用管理令牌代领**。

| 方案 | 做法 | 优点 | 缺点 | 建议 |
|---|---|---|---|---|
| A. 只发链接 | 返回签到页链接 + 今日状态文案 | 零改动、零风险、不破坏任何账务 | 不是“群内一键签到” | 未来可选 |
| B. 服务端桥接 | 新增 `POST /api/bot/v1/users/:id/checkin`，内部调用 `model.ClaimXingyaCheckin(userID, time.Time{})` | 完整复用钱包/批次/轮次/防重复；可审计 | 需要改 NewAPI 并部署；等于对机器人放开自动化签到 | 未来推荐（需签字） |
| C. 插件本地签到 | 机器人自己记每日一次，用 `add_quota` 发普通余额 | 不改服务端 | 与站内签到重复领取；绕过 30 天过期；普通余额 vs 签到钱包口径混乱；违背“仅浏览器会话”的防自动化设计 | 不推荐 |
| D. 不做 | 只保留绑定/余额 | 最省事 | 少一个增长点 | **本期选择** |

**本期选择 D（不做）**；未来若重启：A（先上）→ B（获批后做）。
绝不选“直连 PostgreSQL 写 `users.quota`”。

桥接接口的安全要求（若做 B）：

- `X-Bot-Id` + `X-Bot-Timestamp` + `X-Bot-Signature`（HMAC-SHA256）；
- 时间窗 60 秒 + nonce 防重放；
- 源 IP 白名单（AstrBot 服务器出口 IP）；
- 仅允许“绑定关系存在”的用户 ID（由机器人侧保证）；
- 审计日志记录 actor = `qqbot`，不记录 QQ 号以外的隐私；
- 单用户每分钟限流；
- 密钥只放环境变量，不进仓库。

### 3.5 `/模型状态` —— 已实现（近 30 分钟真实错误）

参考成熟监控面板的做法，不直接拿“渠道启用状态”当模型健康度，而是看真实请求日志；
同时修正两个常见误报：

1. **排除 400-499 客户端错误**：用户参数错、额度不足、鉴权失败等不代表模型挂了；
2. **同一用户的重复错误去重**：一个用户死循环重试 100 次，不能把模型打成异常。

数据来源：

- `GET /api/log/?type=5&start_timestamp=...&end_timestamp=...`（需要 `log:read`）：
  错误日志，`other.status_code` / `other.error_code` 用于分类；
- `GET /api/perf-metrics/summary?hours=1`：近 1 小时成功率，仅作展示，不参与判定。

**前提**：站点环境变量 `ERROR_LOG_ENABLED=true`。rc50 默认是 `false`，未开启时
`/api/log/?type=5` 为空，命令会明确提示，而不是误报“正常”。

判定规则：

| 不同用户报错数 | 状态 |
|---|---|
| 0 | ✅ 正常（或窗口内无数据） |
| 1 | ⚠️ 波动（可能是单用户重试） |
| ≥ `abnormal_users`（默认 2） | ❌ 异常 |

其他规则：

- 统计窗口默认 30 分钟，可配置；
- 错误日志每页 100 条，默认最多 5 页（500 条），超出标注“样本”；
- 4xx 只计数、不参与状态判定，输出里显示“已忽略”；
- 结果缓存 60 秒，避免刷管理 API；
- 不输出渠道名、key、上游地址、用户名或错误正文。

示例输出：

```
📡 星芽模型状态（近 30 分钟）
🕒 统计时间：11-10 14:05

❌ 异常（多个用户报错）
• claude-sonnet-4 — 3 个用户 / 12 次，最后 14:03，主要 502
⚠️ 波动（仅 1 个用户，可能是单用户重试）
• gpt-4o — 1 个用户 / 4 次，最后 13:58，主要 500

✅ 正常：12 个模型
ℹ️ 已忽略 4xx 客户端错误：27 条
📈 近 1 小时整体成功率：98.2%（仅供参考）
```

如果 PAT 没有 `log:read`，命令会明确提示补充权限；渠道状态只作为无日志权限时的
降级方案，不作为主路径。

### 3.6 芽点抽奖 —— 已实现

奖品是**芽点**，开奖后发到中奖者绑定的星芽账号普通余额（`add_quota`）。

**用户命令**：

- `/抽奖`：参与当前会话的抽奖（必须先绑定）；
- `/抽奖信息`：查看当前抽奖或最近一次开奖结果。

**管理命令**：

```text
/发抽奖 [时间] [芽点] [人数] [标题]
/开奖 [编号]
/取消抽奖 [编号]
/抽奖管理
/补发抽奖 [编号]
```

示例：

```text
/发抽奖                          # 默认 30 分钟后，100 芽点，1 人
/发抽奖 100 3                    # 100 芽点，3 人，30 分钟后
/发抽奖 21:30 100 1 今晚福利
/发抽奖 2026-11-10T21:30 100 3 周末福利
/发抽奖 2h 50 2 下午茶
```

**设计要点**：

| 项 | 处理 |
|---|---|
| 参与资格 | 必须已绑定星芽账号；每个平台用户每场只能参与一次 |
| 奖品拆分 | 总芽点平均分给中奖者，余数给前面的中奖者；每人至少 1 芽点 |
| 人数不足 | 实际参与人数少于设定人数时，所有符合条件者中奖 |
| 解绑/换绑 | 开奖时重新校验绑定关系，不满足条件的参与者剔除 |
| 开奖原子性 | `UPDATE ... WHERE status='open'` 抢占，成功后写入中奖记录，防止重复开奖 |
| 自动开奖 | 后台任务每 15 秒检查；机器人重启后补开到点的抽奖 |
| 结果通知 | 主动发送到创建抽奖的会话；发送失败可 `/抽奖信息` 查询 |
| 发放失败 | 中奖记录标记 `failed/unknown`，管理员 `/补发抽奖` 重试 |
| 预算保护 | 最大人数、单次最大芽点、最长预约天数均可配置 |

**合规**：只使用虚拟芽点，不涉及真实货币、现金兑换或提现。

---

## 4. 总体架构

### 4.1 架构图

```
QQ 群/私聊
   │  (OneBot / QQ 官方 / 其他适配器)
AstrBot
   │  fineapi-qqbot 插件
   ├── 命令层（绑定 / 余额 / 签到 / 模型状态 / 管理）
   ├── 领域层（绑定策略 / 首绑礼 / 签到策略 / 状态聚合）
   ├── 基础设施
   │     ├── NewAPI Admin Client（aiohttp，PAT）
   │     ├── SQLite（绑定关系 + 幂等状态 + 冷却）
   │     └── 缓存 / 限流 / 日志脱敏
   ▼
NewAPI /api/*（经 Cloudflare 或源站）
   ├── GET  /api/user/:id
   ├── POST /api/user/manage      （add_quota）
   ├── GET  /api/channel/*
   └── POST /api/bot/*            （v0.2，可选）
```

### 4.2 项目结构（已落地）

仓库根目录**就是插件目录**，AstrBot 克隆后直接加载根目录的 `main.py`：

```
fineapi-qqbot/                     # 本地项目目录
├── main.py                        # Star 入口 + 命令注册 + 事件编排
├── metadata.yaml                  # AstrBot 插件元数据
├── _conf_schema.json              # 可视化配置
├── requirements.txt               # aiohttp
├── __init__.py
├── fineapi_core/
│   ├── client.py                  # NewAPI 客户端：重试、超时、429、脱敏日志
│   ├── store.py                   # SQLite：绑定、台账、抽奖
│   ├── quota.py                   # quota ⇄ USD ⇄ 芽点 换算
│   ├── model_status.py            # 30 分钟错误日志聚合
│   └── lottery.py                 # 开奖时间解析、奖品拆分、抽签
├── docs/
│   └── FEASIBILITY.md
└── tests/
    ├── test_core.py
    ├── test_client.py
    ├── test_lottery.py
    └── test_model_status.py
```

> GitHub 仓库名为 `astrbot_plugin_fineapi_qqbot`，克隆到
> `AstrBot/data/plugins/astrbot_plugin_fineapi_qqbot/` 后即可被 AstrBot 识别。
> 本地文件夹叫 `fineapi-qqbot` 不影响安装，仓库名才决定插件目录名。

### 4.3 插件命名

AstrBot 插件市场推荐目录名以 `astrbot_plugin_` 开头，且全小写、不含空格：

- 本地项目目录：`fineapi-qqbot`；
- **GitHub 仓库名 / 插件目录名**：`astrbot_plugin_fineapi_qqbot`（安装时必须是这个目录名）；
- `metadata.yaml` 里：

```yaml
name: astrbot_plugin_fineapi_qqbot
display_name: 星芽助手
short_desc: 绑定星芽账号、查余额、领首绑礼、看模型状态、芽点抽奖
desc: 在 QQ 群/私聊绑定星芽（xingya.site）账号，查询余额与近 30 分钟模型状态，参与芽点抽奖
help: 发送 /芽点帮助 查看命令
version: 0.3.2
author: fineapi
astrbot_version: ">=4.9.2,<5"
support_platforms:
  - qq_official
  - qq_official_webhook
  - aiocqhttp
```

> 如果坚持仓库名/目录名都用 `fineapi-qqbot`，AstrBot 也能加载，但插件市场体验和规范会差一些。
> 建议：**仓库 `fineapi-qqbot`，插件目录 `astrbot_plugin_fineapi_qqbot`**。

---

## 5. 关键设计

### 5.1 命令清单（MVP）

| 命令 | 别名 | 权限 | 说明 |
|---|---|---|---|
| `/绑定 <用户名> <ID>` | `/绑定ID` | 用户 | 用户名在前、ID 在后；两个参数都匹配才绑定；不保存密码 |
| `/解绑` | `/取消绑定` | 用户 | 只解除本地绑定，不动站点账号 |
| `/领取首绑礼` | `/首绑礼` | 用户 | 重试失败的首绑礼（幂等；已发放/状态未知时拒绝） |
| `/抽奖` | `/参与抽奖` | 用户 | 参与当前会话的抽奖 |
| `/抽奖信息` | `/抽奖详情` | 用户 | 当前抽奖 / 最近开奖结果 |
| `/发抽奖 [时间] [芽点] [人数] [标题]` | `/创建抽奖` | 管理员 | 创建抽奖，不填用配置默认值 |
| `/开奖 [编号]` | `/立即开奖` | 管理员 | 立即开奖 |
| `/取消抽奖 [编号]` | — | 管理员 | 取消未开奖抽奖 |
| `/抽奖管理` | `/抽奖列表` | 管理员 | 最近抽奖记录 |
| `/补发抽奖 [编号]` | — | 管理员 | 补发未到账奖励 |
| `/余额` | `/查询余额`、`/我的额度` | 用户 | 余额 / 累计消耗 / 调用次数 |
| `/模型状态` | `/模型监控`、`/模型可用性`、`/modelstatus` | 用户 | 近 30 分钟异常：排除 4xx，按用户去重，≥2 用户报错才算异常 |
| `/我的ID` | `/平台ID` | 用户 | 查看平台用户 ID / 群 ID（配置管理器和群白名单用） |
| `/用户命令` | `/用户帮助` | 用户 | 只显示用户命令 |
| `/管理命令` | `/管理帮助` | 管理员 | 只显示管理命令 |
| `/芽点帮助` | `/帮助` | 用户 | 完整帮助；管理部分仅管理员可见 |
| `/强制解绑 <平台用户ID>` | — | 管理员 | 删除绑定 |
| `/绑定查询 <平台用户ID或星芽ID>` | — | 管理员 | 查绑定关系与送礼状态 |
| `/补发首绑礼 <平台用户ID> [force]` | — | 管理员 | 幂等补发；状态异常需 `force` |
| `/送礼统计 [日期]` | — | 管理员 | 发放/拦截数、原因分布 |
| `/等级探针 @某人` | — | 管理员 | 打印协议端返回的等级/入群字段，用于上线前校准 |
| `/刷新模型缓存` | — | 管理员 | 清缓存（v0.2） |

> `/签到` 暂不实现；配置里保留 `checkin.mode=off` 的未来扩展位，但不注册命令，
> 避免用户误以为可用。
>
> **官方机器人注意**：`get_sender_id()` 返回 OpenID，不是 QQ 号；同一个人在不同群
> 可能是不同 OpenID。管理命令使用“平台用户 ID”，管理员建议把自己的私聊 C2C OpenID
> 填入 `access.admin_qqs`（或配置为 AstrBot 管理员）。

设计细节：

- 支持 `filter.command` + 别名；建议同时支持无前缀触发（可配置）。
- 敏感命令（绑定验证、余额）默认私聊；群内返回简版或提示私聊。
- 每个 QQ 的查询类命令做 5 秒级限流。
- 所有回复使用 `event.plain_result()` 或 `event.send()`，长文本自动分段。

### 5.2 绑定状态机

```
未绑定 --/绑定 用户名 id--> 校验 ID+用户名/角色/状态/占用
                         │
                         ├─ 失败 → 统一提示，不落库
                         └─ 成功 → 写入绑定(gift_state=none)
                                     │
                                     └─ 礼物判定
                                         ├─ 关闭/已领 → 完成
                                         └─ 开启 → add_quota
                                                    ├─ 成功 → granted
                                                    ├─ 明确失败 → failed，可 /领取重试
                                                    └─ 超时/无响应 → unknown，人工核对
```

`/解绑`：

- 仅删除本地记录；
- 若存在未完成的礼物，先核对再删除；
- 解绑后是否允许再次绑定同一账号：可配置（默认允许，但礼物不重复发）。

### 5.3 可选：服务端绑定码（v0.2）

如果要把 L0 升级到 L2，服务端只需两个小接口：

```
POST /api/user/bind-code        # 登录用户生成 6 位码，5 分钟有效，一次性
POST /api/bot/bind-code/redeem  # 机器人用密钥兑换 {code} → {user_id}
```

- 绑定码只存哈希 + 过期时间 + 使用标记；
- 单用户每小时最多生成 5 次；
- 兑换接口用 HMAC + IP 白名单；
- 这样用户只需在 QQ 里输入 6 位码，不需要交出密码或令牌。

### 5.4 签到策略（暂缓，仅存档）

**未来方案 A：`checkin.mode = "link"`（只发链接）**

```
☀️ 星芽每日签到
请打开：https://xingya.site/console/personal?tab=checkin
签到额度当日有效，30 天过期，先到先得。
```

优点：零改动、零风险；也不会和站内签到冲突。

**未来方案 B：`checkin.mode = "bridge"`（服务端桥接）**

- 机器人 → `POST https://xingya.site/api/bot/v1/users/{id}/checkin`
- 服务端：

```go
// 伪代码，新增 controller + route
func BotClaimCheckin(c *gin.Context) {
    // 1. 校验 HMAC / 时间戳 / nonce / IP
    // 2. 校验 userID 存在且未被封禁
    // 3. 直接复用现有服务
    grant, already, err := model.ClaimXingyaCheckin(userID, time.Time{})
    // 4. 记录审计：actor=qqbot, qq=...
    // 5. 返回 quota_awarded / already_claimed / 下次可领时间
}
```

收益：完全复用钱包、批次过期、轮次、分组、限流，不存在“两套签到”。

**未来方案 C：`checkin.mode = "local"`（不推荐）**：

- 只做群内独立“打卡奖励”，明确叫“群打卡”，金额与站内签到无关；
- 必须单独预算、单独月度上限、单独公示；
- 不能写 `users.quota`，只能用 `add_quota`；
- 必须关闭或公告“站内签到与群打卡同时存在”。

### 5.5 模型状态数据流

```
/模型状态 → 缓存(60s)
          → 计算窗口 [now-30min, now]
          → 分页拉取 GET /api/log/?type=5（最多 max_pages × 100 条）
          → 解析 other.status_code / error_code
          → 排除 400-499
          → 按 model + 去重用户聚合
          → GET /api/perf-metrics/summary?hours=1（仅展示成功率）
          → 文本输出
```

- 只输出模型名、不同用户数、错误次数、最后时间、主要状态码；
- 不展示渠道名、key、上游地址、用户名、错误正文；
- 同一用户重复错误只算 1 个用户；
- 日志超过采样上限时明确标注“样本”；
- 结果缓存 60 秒，避免刷日志接口。

### 5.6 配置 Schema（草案）

完整配置见插件目录的 `_conf_schema.json`，MVP 分组：

```json
{
  "site": {
    "base_url": "https://xingya.site",
    "access_token": "<管理员 PAT>",
    "admin_user_id": 0,
    "timeout_seconds": 10,
    "quota_per_unit": 500000
  },
  "binding": {
    "allow_group": true,
    "allow_private": true,
    "block_admin": true
  },
  "gift": {
    "enabled": true,
    "amount_usd": 1.0,
    "new_user_id_after": 8352
  },
  "lottery": {
    "enabled": true,
    "default_duration_minutes": 30,
    "default_prize_yadian": 100,
    "default_winners": 1,
    "max_winners": 20,
    "max_prize_yadian": 10000,
    "max_duration_days": 30,
    "auto_draw_interval_seconds": 15
  },
  "model_status": {
    "enabled": true,
    "window_minutes": 30,
    "max_pages": 5,
    "abnormal_users": 2,
    "exclude_client_errors": true,
    "top_n": 8,
    "show_ok": false,
    "admin_only": false,
    "cache_seconds": 60
  },
  "access": {
    "admin_qqs": [],
    "whitelist_groups": [],
    "query_cooldown_seconds": 3
  },
  "ui": {
    "show_yadian": true,
    "show_cny": false,
    "exchange_rate": 7.2,
    "private_balance_only": false
  }
}
```

### 5.7 数据模型（SQLite）

```sql
CREATE TABLE IF NOT EXISTS bindings (
  qq              TEXT PRIMARY KEY,
  newapi_user_id  INTEGER NOT NULL UNIQUE,
  username        TEXT NOT NULL,
  bound_at        INTEGER NOT NULL,
  gift_state      TEXT NOT NULL DEFAULT 'none',  -- none|pending|granted|failed|unknown|skipped
  gift_quota      INTEGER NOT NULL DEFAULT 0,
  gift_at         INTEGER,
  gift_reason     TEXT,
  updated_at      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS op_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  qq          TEXT,
  action      TEXT NOT NULL,
  detail      TEXT,
  created_at  INTEGER NOT NULL
);

-- 独立发放台账：解绑/重绑不删除，防止重复领取
CREATE TABLE IF NOT EXISTS gift_grants (
  platform_id     TEXT PRIMARY KEY,
  newapi_user_id  INTEGER NOT NULL UNIQUE,
  quota           INTEGER NOT NULL,
  state           TEXT NOT NULL,  -- pending|granted|unknown
  reason          TEXT,
  created_at      INTEGER NOT NULL,
  granted_at      INTEGER
);

CREATE TABLE IF NOT EXISTS lotteries (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  umo            TEXT NOT NULL,
  title          TEXT NOT NULL DEFAULT '',
  prize_yadian   REAL NOT NULL,
  winners        INTEGER NOT NULL,
  draw_at        INTEGER NOT NULL,
  created_at     INTEGER NOT NULL,
  created_by     TEXT,
  status         TEXT NOT NULL DEFAULT 'open',  -- open|drawn|cancelled
  drawn_at       INTEGER
);

CREATE TABLE IF NOT EXISTS lottery_entries (
  lottery_id      INTEGER NOT NULL,
  platform_id     TEXT NOT NULL,
  newapi_user_id  INTEGER NOT NULL,
  username        TEXT NOT NULL,
  joined_at       INTEGER NOT NULL,
  PRIMARY KEY (lottery_id, platform_id)
);

CREATE TABLE IF NOT EXISTS lottery_winners (
  lottery_id      INTEGER NOT NULL,
  platform_id     TEXT NOT NULL,
  newapi_user_id  INTEGER NOT NULL,
  username        TEXT NOT NULL,
  quota           INTEGER NOT NULL,
  state           TEXT NOT NULL,  -- pending|granted|failed|unknown
  reason          TEXT,
  granted_at      INTEGER,
  PRIMARY KEY (lottery_id, platform_id)
);
```

- WAL 模式，`busy_timeout=3000`；
- 所有写操作串行化，避免并发丢写；
- `gift_state`：`pending` 只在请求期间存在；明确失败 → `failed`；
  超时/无响应 → `unknown`（禁止自动重试）；启动时把超时 10 分钟的 `pending` 标为 `unknown`。
- `gift_grants` 是**独立于绑定关系的发放台账**：
  - `platform_id` 唯一（一个平台用户只能领一次）；
  - `newapi_user_id` 唯一（一个星芽账号只能领一次）；
  - `/解绑` 不删除台账，重新绑定后 `/绑定` 会提示“已经领取过”，不会重复发；
  - 发放前先写 `pending` 占位，成功改 `granted`，明确失败删除占位，超时改 `unknown`。

数据目录：`data/plugin_data/astrbot_plugin_fineapi_qqbot/`（通过 AstrBot 数据目录 API 解析），
不要写插件目录（更新会覆盖）。

### 5.8 NewAPI 客户端要点

- 只用 `aiohttp`（AstrBot 规范禁止 `requests`）；
- 统一超时（默认 10s）、最多 2 次重试（只对网络错误/5xx）；
- 429 时指数退避 + 全局冷却；
- 日志脱敏：token、用户邮箱、IP 一律不打印；
- 解析统一：`success=false` 时保留 `message` 但只回显用户可读部分；
- 每个命令最多 1–2 次站点请求；
- 不在群消息里回显完整 API 响应。

### 5.9 安全与合规

| 风险 | 控制 |
|---|---|
| 管理员 PAT 泄露 | 最小 scope：`user:read` + `user:write`（送礼）+ `log:read`（模型状态）；`secret: true`；不写日志 |
| 绑定冒用 | 唯一约束 + 管理员保护 + 后续绑定码；不提供转账前保持 L0 |
| 首绑礼刷取 | 每用户/每 QQ 一次；账号年龄门槛；幂等状态机 |
| 群消息泄露余额 | 群内简版/私聊；可配置完全私聊 |
| 管理 API 被刷 | 每 QQ 限流 + 结果缓存 |
| 签到自动化争议 | 默认只发链接；桥接需明确授权并审计 |
| 账号被封/删 | 每次操作前检查 `status`；失败给出明确提示 |
| 合规 | 不做真实货币兑换、不做赌博玩法；抽奖仅虚拟额度且需公示概率与上限 |

---

## 6. 开发排期（建议）

| 阶段 | 内容 | 预估 | 依赖 |
|---|---|---|---|
| M0 | 项目骨架、metadata、配置 schema、客户端封装、SQLite | ✅ 已完成 | — |
| M1 | `/绑定 <用户名> <ID>`、`/解绑`、`/余额`、首绑礼 + 幂等 + 管理命令 | ✅ 已完成 | M0 |
| M2 | 帮助/文案、异常处理、管理命令、单元测试 | ✅ 已完成 | M1 |
| M3 | `/模型状态`（30 分钟错误日志 + 排除 4xx + 用户去重 + 缓存） | ✅ 已完成 | M1 |
| M4 | 真机联调、打包发布 | 0.5–1 天 | M1–M2 |
| M5 | 芽点抽奖（创建/参与/定时开奖/补发） | ✅ 已完成 | M1 |
| M6（可选） | 签到桥接 / 绑定码（暂缓，需另行授权） | 0.5–1 天（Go） | 你授权 |

MVP（M0–M2）代码已落地；剩余是真机联调和发布。
签到不在本期范围。

---

## 7. 验收清单

- [ ] `/绑定 alice 123` 成功；`/绑定 bob 123`（用户名不匹配）被拒
- [ ] 兼容旧格式 `/绑定 123 alice`，并提示新格式
- [ ] 绑定不存在/管理员/封禁账号有明确提示
- [ ] 同一 QQ、同一星芽用户均不能重复绑定
- [ ] `/余额` 数字与站内后台一致（quota / USD / 芽点换算正确）
- [ ] 首绑礼只发一次；杀掉插件重启不会重复发
- [ ] ID ≤ `gift.new_user_id_after` 的老用户不发放，标记 `skipped`，且绑定成功消息不出现首绑礼提示
- [ ] ID > 阈值的新用户正常发放；`/领取首绑礼` 同样按阈值判断
- [ ] 管理员 `/补发首绑礼 <平台用户ID> force` 可人工覆盖老用户限制
- [ ] 明确失败时标记 `failed`，`/领取首绑礼` 可重试
- [ ] 超时/无响应时标记 `unknown`，不自动重试，管理员可 `force` 补发
- [ ] `/解绑` 后本地记录删除，站点账号不受影响
- [ ] 群白名单、私聊限制、每 QQ 查询冷却生效
- [ ] `/发抽奖` 不填参数使用默认值；支持 `30m`、`21:30`、`2026-11-10T21:30`
- [ ] 未绑定用户不能参与；同一用户重复参与被拒
- [ ] 到点自动开奖；重启后补开；同一抽奖不会重复开奖
- [ ] 奖品按人数拆分，余数分配正确，总和不超发
- [ ] 开奖后主动消息发送到创建会话；失败时 `/抽奖信息` 可查最近结果
- [ ] 发放失败可 `/补发抽奖`；`/抽奖管理` 能看到状态与参与人数
- [ ] `/模型状态` 只统计 30 分钟窗口；4xx 被忽略；单用户重复错误只算 1 个用户
- [ ] 2 个不同用户报错才显示“异常”，1 个用户显示“波动”
- [ ] 错误日志超过 500 条时输出标注“样本”，且 60 秒内走缓存
- [ ] 缺少 `log:read` 时明确提示补充权限
- [ ] 日志中无 token、无密码、无完整用户隐私
- [ ] Cloudflare/WAF 下连续调用 100 次无 403/429 异常放大
- [ ] AstrBot 热重载/重启后数据不丢

---

## 8. 风险登记

| 风险 | 等级 | 触发条件 | 应对 |
|---|---|---|---|
| 首绑礼重复发放 | 中高 | API 成功但本地未落库 | pending → granted；超时标 unknown，不自动重试 |
| 首绑礼被批量薅 | 中高 | 大量小号 | **已接受**（官方机器人无法取等级/IP）；用活动预算和事后封禁控制 |
| 绑定冒用/抢占 | 中 | L0.5 用户名非秘密 | 唯一约束 + 管理员解绑；转额度前升级绑定码 |
| 签到口径冲突 | 低（本期不做） | 未来启用 `local` 模式 | 只走官方服务/链接，绝不直写库 |
| 抽奖重复开奖 | 中高 | 并发/重启 | `status='open'` 原子抢占 + 中奖记录唯一键 |
| 抽奖超发/误操作 | 中高 | 管理员填错金额 | 单次上限 + 人数上限 + 至少每人 1 芽点 |
| 主动消息发送失败 | 中 | 平台限制 | 开奖仍入库；`/抽奖信息` 可查；日志告警 |
| 模型状态误报 | 中 | 单用户重试/客户端 4xx | 排除 4xx + 按用户去重 + ≥2 用户才算异常 |
| 日志量过大 | 中 | 30 分钟错误日志 > 500 条 | 分页上限 + 样本标注 + 60 秒缓存 |
| Cloudflare 拦截机器人 | 中 | WAF/Bot 规则 | 出口 IP 白名单/源站直连 |
| 管理 PAT 权限过大 | 中 | 用了 root 全量令牌 | 最小 scope，单独建机器人管理员 |
| API 变更 | 低中 | NewAPI 升级 | 只依赖稳定管理接口；集成测试 |
| 余额泄露 | 低中 | 群内直接展示 | 私聊/脱敏/可配置 |
| AstrBot 版本差异 | 低 | 旧版加载失败 | `astrbot_version` 声明 + 兼容层 |

---

## 9. 推荐的可选玩法（MVP 之后）

按“投入/风险/增长”排序：

1. **绑定抽奖**（推荐）
   - 绑定成功后获得一次抽奖机会，奖品为芽点/兑换码；
   - 概率、奖池、每日上限全部后台可配；
   - 仅虚拟额度，不涉及真实货币；公示概率。
2. **签到日历图**
   - 用 AstrBot 转图能力渲染本月签到日历；
   - 数据来自官方签到状态（桥接模式下）。
3. **邀请/绑定排行榜**
   - 群内绑定人数、活跃调用、消费排行；
   - 只读接口，注意隐私。
4. **余额预警**
   - 定时检查绑定用户余额，低于阈值私聊提醒（需用户授权）。
5. **用量日报/周报**
   - 调用次数、消耗、Top 模型；数据来自日志接口（需 `log:read`）。
6. **模型状态订阅**
   - 渠道自动禁用/恢复时推送群消息（轮询 + 去重）。
7. **兑换码/福利码**
   - 管理员发码，用户在 QQ 兑换；必须服务端幂等，不建议 MVP。
8. **群打卡（独立于站内签到）**
   - 明确命名、独立预算、独立上限；不要与官方签到混用。

> 不建议在 MVP 引入：红包、转账、抢劫、押注类玩法。它们需要真实扣款/入账的原子事务与合规评估，参考项目的做法在星芽上会破坏钱包与审计。

---

## 10. 待你确认的问题

**已确认（2026-11-10）**：

- ✅ 绑定：`/绑定 <用户名> <ID>`；
- ✅ 签到：暂不做；
- ✅ 首绑礼：普通余额；
- ✅ 防薅：取消（QQ 官方机器人拿不到等级/入群时长，星芽也不存注册 IP）。

**仍需确认 / 联调**：

1. **管理令牌**：是否已创建只含 `user:read` / `user:write` 的管理员 PAT？
2. **部署位置**：AstrBot 是否与 NewAPI 同机/同内网？机器人出口 IP 是否可能被 Cloudflare 拦截？
3. **使用场景**：只在内测群，还是公开群？是否需要群白名单？
4. **首绑礼预算**：$1 单次是否合适？是否需要活动总预算/每日上限（可在站点侧控制）？
5. **项目命名**：仓库 `fineapi-qqbot` + 插件目录 `astrbot_plugin_fineapi_qqbot` 是否接受？

---

## 附录 A：关键接口速查

| 用途 | 方法 | 路径 | 权限 / scope |
|---|---|---|---|
| 查询用户 | GET | `/api/user/:id` | Admin + `user:read` |
| 搜索用户 | GET | `/api/user/search?keyword=` | Admin + `user:read` |
| 调整额度 | POST | `/api/user/manage` | Admin + `user:write` |
| 启用模型 | GET | `/api/channel/models_enabled` | Admin + `channel:read` |
| 渠道列表 | GET | `/api/channel/?p=0&page_size=200` | Admin + `channel:read` |
| 错误日志 | GET | `/api/log/?type=5&start_timestamp=&end_timestamp=` | Admin + `log:read` |
| 性能汇总 | GET | `/api/perf-metrics/summary?hours=1` | 登录/公开（模块开启时） |
| 价格/模型表 | GET | `/api/pricing` | 公开（模块开启时） |
| 站点信息 | GET | `/api/status` | 公开 |
| 运行状态 | GET | `/api/uptime/status` | 公开（需配置） |
| 签到（官方） | POST | `/api/user/xingya-daily-claim` | **浏览器会话** + Turnstile（PAT 不可用） |
| 签到状态 | GET | `/api/user/xingya-checkin` | `wallet:read`，仅本人数据 |

请求头：

```
Authorization: Bearer <PAT>
Content-Type: application/json
```

> `New-Api-User: <管理员用户ID>` 仅旧版 NewAPI 需要；rc50 可省略。

## 附录 B：参考项目 → 本项目映射

| 参考项目 | 本项目 | 说明 |
|---|---|---|
| `Store`（JSON） | `core/store.py`（SQLite） | 事务、唯一约束 |
| `MySQLQuota` | 删除 | 星芽 PostgreSQL，禁止直写 |
| `NewAPIClient` | `core/client.py` | 保留重试/超时，增加 scope 与脱敏 |
| `/绑定 <ID>` 两步验证 | `/绑定 <用户名> <ID>` 单步校验 | 不保存密码 |
| `/余额` | `/余额` | 增加芽点换算与缓存 |
| `/签到` | `/签到`（link/bridge） | 不直写库 |
| `/查用户` | `/绑定查询` | 只读 |
| `/强制解绑` | `/强制解绑` | 保留 |
| 游戏/红包/股市 | 不做（MVP） | 合规与账务风险 |
| 单文件 | 分层 | 可测试、可维护 |

---

## 附录 C：参考资料

- AstrBot 插件开发：https://docs.astrbot.app/dev/star/plugin-new.html
- 插件配置：https://docs.astrbot.app/dev/star/guides/plugin-config.html
- 参考项目：`cankao/astrbot_plugin_newapi_helper`（README、`main.py`、`_conf_schema.json`）
- 星芽源码：`xingya-newapi-new/controller/user.go`、`user_quota.go`、`xingya_checkin.go`、`router/api-router.go`
- 星芽记忆：`.agents/MEMORY.md`（签到钱包、30 天有效期、100 芽点/USD）
