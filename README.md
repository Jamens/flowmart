# flowmart

电商后端 + **数据库驱动的可配置工作流引擎**。

核心思路：把订单状态流转从代码里搬到数据库中。流程定义存在 `wf_definitions / wf_nodes / wf_transitions` 三张表里，
引擎在运行时读取它们决定「下一步能去哪」。因此**新增审批环节、调整流转条件都不需要改后端代码**。

## 技术栈

- 后端：Python 3.13 / FastAPI / SQLAlchemy 2.0 / Pydantic v2
- 数据库：默认 SQLite（开发查看方便），可切换 MySQL
- 流程条件求值：simpleeval（安全的表达式求值，不用 `eval`）
- 测试：pytest

## 快速开始

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 建表（默认在项目根生成 flowmart.db）
python backend/scripts/init_db.py

# 3. 灌入演示数据（流程定义 + 商品 + 各种状态的订单）
python backend/scripts/seed.py --reset

# 4. 启动后端
cd backend && python -m uvicorn app.main:app --reload

# 5. 启动前端（另开一个终端）
cd frontend && npm install && npm run dev
```

- Swagger 接口文档：http://127.0.0.1:8000/docs
- 管理后台：http://127.0.0.1:5173（含订单管理与流程设计器）

前端已通过 Vite proxy 把 `/api` 转发到后端，开发期无需额外配置跨域。

- **生产部署注意**：浏览器走 Cookie 鉴权时，必须设置 `CORS_ORIGINS` 为真实前端域名（不再用 `*`），
  并把 `COOKIE_SECURE` 设为 `True`（仅 HTTPS 下浏览器才接受 `HttpOnly + Secure` 的 Cookie）。
  `logout` 清 Cookie 的 `secure`/`samesite` 与 `set_auth_cookie` 严格一致，否则生产环境清不掉。

## 数据库切换

默认 SQLite（文件 `flowmart.db`）。切到 MySQL 只需设置环境变量或 `.env`：

```bash
DB_DIALECT=mysql
DB_PASSWORD=1234560
```

## 数据库迁移（Alembic）

模型改了之后**不要再靠 `--drop` 重建**（会丢数据），改用迁移：

```bash
cd backend

# 1. 改完模型后生成迁移脚本
python -m alembic revision --autogenerate -m "简述改了什么"

# 2. 检查将要执行的 SQL（可选，推荐先看一眼）
python -m alembic upgrade head --sql

# 3. 应用到数据库
python -m alembic upgrade head

# 回滚一个版本
python -m alembic downgrade -1
```

要点：

- **连接串不在 `alembic.ini` 里**，由 `migrations/env.py` 从 `app.core.config.settings`
  读取，`DB_DIALECT` / `.env` 仍是唯一事实来源。临时迁移别的库可用
  `python -m alembic -x url="sqlite:///D:/tmp/x.db" upgrade head`。
- **SQLite 必须开 batch 模式**（env.py 已开）：SQLite 不支持多数 `ALTER`，
  不开的话改列会直接报错。
- **`alembic.ini` 必须保持 ASCII**：中文 Windows 下 alembic 用 GBK locale 读它，
  写中文注释会直接 `UnicodeDecodeError`。
- **`init_db.py` 与 alembic 可共存**：`init_db` 走 `create_all` 后会自动打上
  当前版本标记（baseline stamp），否则之后 `alembic upgrade` 会把库当空库、
  重新建表并报「table already exists」。
- MySQL 首次使用需先跑 `init_db.py`（它会建库），再由 alembic 管表结构。

### 发布前：把迁移在真实 MySQL 上跑一遍

`backend/tests/test_migrations.py` 默认跑临时 SQLite，但它**可以指向任意库**：

```bash
# 用一个专门的库（会建表/删表，别指向有数据的库）
MIG_TEST_URL="mysql+pymysql://root:密码@127.0.0.1:3390/flowmart_mig" \
  python -m pytest backend/tests/test_migrations.py -q
```

它验证三件事：`upgrade head` 后模型里的表一张不少、`alembic check` 断言**模型与迁移一致**
（专抓「改了模型却没生成迁移」）、`downgrade base` 能真正拆掉表（迁移是双向可用的，不是单向门）。

为什么不能只验 SQLite：**两者的执行路径完全不同**。SQLite 走 `render_as_batch`
（新建表 → 拷数据 → 删旧表 → 改名），MySQL 走原生 `ALTER`。
只验 SQLite 会漏掉「某步 ALTER 在 MySQL 上不支持」这类只在发布当天爆炸、
且那时回滚窗口最窄的问题。

> **已实跑（MySQL 8.0）：发现并修复了一个只在 MySQL 暴露的回滚 bug。**
> autogenerate 生成的 `downgrade()` 习惯「先 `drop_index` 再 `drop_table`」，
> 但 MySQL 的 InnoDB 里**外键依赖索引**，外键还在时删索引会被直接拒绝：
> `Cannot drop index 'ix_refresh_tokens_user_id': needed in a foreign key constraint`。
> 而 `DROP TABLE` 本来就会连带删掉索引与外键，那几步纯属多余且有害。
> 三个迁移文件的 `downgrade` 已去掉这些冗余步骤，并在注释里写明原因
> （否则下次 autogenerate 又会长回来）。
> 修复前：`downgrade base` 在 MySQL 上直接失败；修复后：升 → 降 → 升全部通过。

这一步也进了 CI：`.github/workflows/ci.yml` 的 `migrations` job 用 MySQL 服务容器
自动跑同一件事，所以「改了模型/迁移但只在 SQLite 上验过」会被拦在合并之前。

## 已完成功能

### 工作流引擎（`app/services/workflow_engine.py`）

- `start()` 启动流程实例，初始停在 start 节点
- `fire()` 按「当前节点 + 事件 + 条件」匹配流转边，多条候选按 priority 取第一条满足条件的
- `available_events()` 返回当前可执行事件，供前端渲染按钮
- 每次流转自动写 `wf_transition_logs`，并保存当时的上下文快照用于事后复盘
- 到达 end 节点自动将实例置为 finished
- 非法流转显式报错，不静默忽略

### 流程定义版本管理（`app/api/workflows.py`）

- 同一 `code` 可有多版本（`version` 递增），但同一时刻**全局唯一 `published`** 生效
- `POST /api/v1/workflows/definitions/{id}/versions` 派生新草案版本：克隆源定义的节点与流转边，`version = max(同 code 版本) + 1`
- `GET /api/v1/workflows/definitions/code/{code}/versions` 查版本历史（按 version 倒序，含 draft/published/archived）
- `POST .../publish` 发布时自动把同 `code` 的其它 `published` 版本降级为 `archived`，**保证唯一 published**
- 回滚 = 把某个旧版本重新 `publish`（旧版本重新生效，新版本被降级）
- `GET /api/v1/workflows/definitions?code=` 支持按业务 code 过滤版本行
- **在途实例隔离**：`WorkflowInstance.definition_id` 钉死各自版本，发布新版本降级旧版本后，正在跑的实例依旧用其 `definition_id` 对应的图继续流转，不受影响
- 无需数据库迁移：模型原本就支持 `code + version + status` 多版本，版本管理是纯 API/行为变更

### 订单服务（`app/services/order_service.py`）

- 创建订单：校验库存 → 算金额 → 扣库存 → 启动流程 → 推进到待付款，**同一事务**
- 副作用与流转解耦：`SIDE_EFFECTS` 按事件名注册（pay 记流水、cancel/refund 归还库存）
- 未注册副作用的新事件（如 approve、reject）默认纯推进，无需改动任何代码
- 订单表冗余 `status` 字段供列表筛选，由引擎同步写入

### 鉴权（`app/core/security.py` + `app/api/auth.py`）

- **JWT（HS256）**：登录/注册签发令牌，后续请求在 `Authorization: Bearer` 头携带；
  访问令牌只含 `sub`（用户 id）、`iat`、`exp`，用 HMAC-SHA256 签名，`hmac.compare_digest` 常量时间校验防时序攻击；刷新令牌另带 `jti` / `fam`（见下方「刷新令牌轮转」）。
- **令牌存储改为 httpOnly Cookie（抗 XSS）**：登录/注册在返回 Bearer 令牌（供 API 客户端）的同时，
  把 JWT 写入 `HttpOnly` Cookie；浏览器同源请求由 Cookie 自动携带（`SameSite=Lax`），前端不再用 `localStorage`
  存明文令牌，从根上杜绝 XSS 脚本窃取令牌。新增 `POST /auth/logout` 由后端下发删除指令清除 Cookie
  （JS 无法删除 HttpOnly Cookie），**并在服务端撤销该登录会话的刷新令牌**——只清 Cookie 的话，
  泄露出去的令牌在 7 天有效期内仍可换发访问令牌。`get_current_user` 优先取 Bearer 头、其次取 Cookie。
- **密码哈希**：PBKDF2-HMAC-SHA256（10 万次迭代）+ 随机盐，存储格式 `pbkdf2$sha256$<iter>$<salt>$<dk>`，
  明文绝不下库；纯标准库实现，无第三方加密依赖。
- **依赖注入取身份**：`get_current_user` 解析令牌返回用户对象，所有资源接口 `Depends` 它，
  因此「当前用户」恒来自令牌，前端无法伪造 `user_id` 冒充他人。
  - 前端下单（`OrdersView.vue` 的 `submitCreate`）只传 `items`，不再带 `user_id`/`address_id` 死字段；即便误带后者后端也忽略，保持归属来源唯一。不传 `address_id` 时订单无收货快照（详情页优雅显示「-」）。
- 登录失败（用户不存在 / 密码错误）统一返回 `401 用户名或密码错误`，不泄露哪些用户名已注册。
- **生产必须设置 `SECRET_KEY`**：`config.SECRET_KEY` 仍为开发默认值时，非 debug 模式启动会直接报错，杜绝「 anyone can forge token 」。
- **RBAC 角色权限（已落地）**：角色分「管理员 / 普通买家」，`User.is_admin` 字段 + `require_admin` 依赖闸门。
  - 用户管理（建/列/禁用账号）**仅管理员** 可执行；订单流转推进**按角色分流**——管理员可对任意订单执行任意事件，买家仅可对自己的订单执行「取消 / 确认收货」；
  - 普通买家只能改**自己**的资料、只看**自己**的订单；越权访问他人资源统一 `404`，不泄露目标是否存在；
  - 购物车、订单创建、地址归属始终严格取自令牌用户；`GET /users/{id}` 不返回收货地址，地址须经归属校验的 `/users/{id}/addresses` 获取。
  - **初始管理员可配置**：`config.BOOTSTRAP_ADMIN`（默认 `zhangsan`，可经环境变量覆盖）指定首次 `seed` / `init_db` 时提升为管理员的账号；`BOOTSTRAP_ADMIN` 为空会在启动时直接报错，避免「谁都不是管理员」导致系统静默锁死。生产务必改成真实管理员账号。
  - **登录限流（防暴力破解）**：`POST /auth/login` 按 `(客户端IP, 用户名)` 固定窗口计数失败次数，窗口内（`LOGIN_RATE_LIMIT_WINDOW`，默认 60 秒）失败超 `LOGIN_RATE_LIMIT_MAX`（默认 5 次）即返回 `429` 并带 `Retry-After` 头；登录成功清空计数，避免正常用户被旧失败数误伤；禁用账号不计失败次数。
    - **可插拔存储**：默认进程内内存（`MemoryStore`，单实例够用、零依赖）；多实例 / 负载均衡设 `LOGIN_RATE_LIMIT_REDIS_URL`（如 `redis://127.0.0.1:6379/0`）即切 Redis 后端（`INCR+EXPIRE` 原子计数，限流对全部实例统一生效），连不上启动时直接报错（fail-fast）。后端接口不变（`app/core/ratelimit.py`）。
    - **客户端 IP 安全默认**：默认 `LOGIN_RATE_LIMIT_TRUST_PROXY=False`，取 `request.client.host`（直连真实 socket 地址、无法伪造）；仅当反向代理已用真实客户端 IP 覆写 `X-Forwarded-For` 且显式开 `True` 时才信 XFF 首跳——否则攻击者可伪造不同 XFF 绕过限流。
    - **`init_db` 演示密码回填仅限开发环境**：`_backfill_demo_passwords` 给无密码用户赋 `123456` 仅当 `DEBUG=True` 生效；生产环境（`DEBUG=False`）**绝不静默写入已知明文密码**（否则迁移 / 外部认证导致的空密码存量账户会被统一接管），仅打印警告，提醒走正式「找回密码」流程。与 `SECRET_KEY` / `COOKIE_SECURE` / `OTP_DEV_RETURN_CODE` 等生产闸门同源。

### 邮箱 / 手机验证（`app/core/verification.py` + `app/api/auth.py`）

- 两步式：`POST /auth/verification/send` 申请一次性 OTP，`POST /auth/verification/confirm` 确认；确认成功后把 target 绑定到账号并置对应渠道的 `*_verified=True`。
- 验证码用 `secrets` 生成（密码学随机，非 `random`）；确认用 `hmac.compare_digest` 常量时间比对，防时序侧信道。
- **防爆破**：单个验证码的确认尝试超过 `OTP_CONFIRM_MAX_ATTEMPTS`（默认 5）即锁定该码（置 `consumed_at`），避免对 6 位码暴力枚举；重发本身另有窗口限流。
- **可插拔发送器（配置驱动）**：`VerificationSender` ABC + `ConsoleSender` / `SmtpSender` / `WebhookSmsSender`，
  邮件与短信**各自独立**选择（`OTP_EMAIL_SENDER` / `OTP_SMS_SENDER`，留空跟随全局 `OTP_SENDER`）。
  默认 `console`，零凭据即可跑通；配了真实发送器却缺连接参数会**启动即报错**。详见下方专节。
- **重发限流基于 DB**（同一用户对同一渠道在时间窗内最多 N 次），天然多实例安全，不依赖进程内内存或 Redis；每次申请都会作废同渠道同 target 的未消费旧码，防重放。
- **开发便利开关**：`OTP_DEV_RETURN_CODE=True`（默认）时 `send` 接口在响应里回传 `dev_code`，便于联调与测试；生产**必须 False**，配置校验会强制（`DEBUG=False` 下仍为 True 即启动报错）。
- 模型新增 `User.email` / `email_verified` / `phone_verified` 三列，并新建 `verification_codes` 表；已生成 Alembic 迁移（与 `alembic check` 守卫一致）。注册接口支持可选 `email`。
- **下单强约束（已落地）**：`POST /api/v1/orders` 与 `POST /api/v1/cart/checkout` **共用同一依赖 `require_verified_contact`**（单一事实来源，杜绝绕过），在 API 层校验「当前用户已验证邮箱或手机**至少其一**」，否则返回 `403`（detail 指引先走 send/confirm）。**服务层 `OrderService.create_order` 不受限**——后台运营/迁移等直接调用路径不应被账户合规约束拦截；验证状态取运行时实时值，撤销验证后会被重新拦截。
- **登录强约束（已落地）**：`POST /api/v1/auth/login` 校验「已验证邮箱或手机**至少其一**」，否则返回 `403`（detail 指引先走验证），与下单闸门同源——未验证账号无法进入系统。为避免把存量未验证用户（含初始管理员 `BOOTSTRAP_ADMIN`）永久锁死，`seed.py` / `init_db._backfill_admin` 已把演示/初始管理员置为 `email_verified=True`；真实用户走下方自助验证即可登录。
- **验证入口允许未登录自助**：`/auth/verification/send` 与 `/confirm` 改用 `get_optional_current_user` + 账号密码自证（`_resolve_verification_user`）——已登录走令牌，未登录在登录前凭 `username`/`password` 自证身份也能申请并确认验证码。否则未验证用户会陷入「验证要令牌 → 没令牌登录被拦 → 永远无法验证」的死锁。

### 通知渠道可配置（`app/core/verification.py`）

本项目是**开源**的，通知这块只定义「怎么连出去」，**不替使用者选厂商**，账号全部由部署者用环境变量填。
邮件与短信各自独立可配，否则没法「邮件走 SMTP、短信走网关」：

| 配置项 | 说明 |
| --- | --- |
| `OTP_SENDER` | 全局默认：`console` / `smtp` / `webhook`，默认 `console` |
| `OTP_EMAIL_SENDER` | 邮件渠道覆盖：留空跟随全局，可选 `console` / `smtp` |
| `OTP_SMS_SENDER` | 短信渠道覆盖：留空跟随全局，可选 `console` / `webhook` |
| `SMTP_*` | `HOST` / `PORT`(587) / `USER` / `PASSWORD` / `FROM` / `STARTTLS` / `USE_SSL`(465) / `TIMEOUT` |
| `SMS_WEBHOOK_*` | `URL` / `METHOD` / `BODY` / `HEADERS` / `TIMEOUT` / `SUCCESS_MIN_STATUS`~`MAX_STATUS` |

三个刻意的选择：

1. **默认 `console`，零凭据可跑**。clone 下来第一次 `python -m uvicorn` 不该被「先去申请短信服务」卡住，
   注册 / 验证全链路就能走通（验证码打在日志里）。代价是生产**不允许**有渠道落到 `console`——
   见「生产环境启动校验」。
2. **邮件用标准库 `smtplib`，零新增依赖**。任何 SMTP 服务商都能连，不需要各家 SDK。
   外呼一律带 `SMTP_TIMEOUT`（默认 10 秒）：SMTP 是同步阻塞调用，不设超时会让上游一次抖动
   把请求线程全部挂住。465 用 SSL 直连、587 用 STARTTLS，两者互斥，配了会启动报错。
3. **短信用通用 HTTP 网关，不内置任何厂商 SDK**。各家签名算法不同且会变，内置等于替使用者选厂商，
   还要把他们的包变成运行时依赖。改为把网关地址配上即可——它可以是厂商网关，也可以是自建的
   一小段转发服务（想接谁就接谁）。报文模板用 `{target}` / `{code}` / `{ttl}` 占位符描述，
   `POST` 时整体作请求体（通常配成 JSON）、`GET` 时按 query string 解析成查询参数。

**发送失败要回滚**：`request_code` 改成「先发送、后落库」，渠道挂掉时整段回滚并转 `502`——
否则这条用户根本没收到的码会占掉重发配额，上游一挂用户就被自己的限流锁死，
且现象是「点了没反应」而非报错。发送器自身也把上游异常统一收敛成 `VerificationError`（502 可重试），
不让第三方异常原样变成 500 堆栈。

### 域名 / 部署环境 / TLS 可配置（`app/core/config.py` + `app/main.py`）

开源项目不能替使用者决定部署形态：同源直跑、反代后面、还是前后端分域，全靠开关表达。
单一事实来源是 `PUBLIC_BASE_URL`（配一次域名尽量复用，别让各处各写一遍）。

| 配置项 | 说明 |
| --- | --- |
| `DEPLOY_ENV` | `dev` / `staging` / `production`，仅标签，出现在 `/health` 便于区分跑的是哪套；`production` 必须与 `DEBUG=False` 同号（否则启动报错） |
| `PUBLIC_BASE_URL` | 站点公网基址（`https://shop.example.com`）：支付回调地址留空时回退到它（域名只配一次），也是 HTTPS 跳转目标 |
| `COOKIE_DOMAIN` | 令牌 Cookie 的 `domain`；留空 = host-only（同源部署）。前后端分处不同子域（如 `api.example.com` 与 `shop.example.com`）时必须设 `.example.com`，否则登录态在子域间失效 |
| `TRUST_PROXY` | 全局反向代理信任：是否可信 `X-Forwarded-For` / `X-Forwarded-Proto`。仅当代理已用真实 IP / 协议覆写这两个头时开 `True` |
| `ENFORCE_HTTPS` | `True` 时把非 https 请求 307 跳到 https（基于请求 host）；**反代后必须同时 `TRUST_PROXY=True`**，否则代理转发的 http 会被无限重定向 |
| `HSTS_MAX_AGE` | >0 时 https 响应加 `Strict-Transport-Security: max-age=...; includeSubDomains`；0 = 不发（生产建议 31536000） |

三个刻意的选择：

1. **协议判定考虑反代**：`_is_secure` 直连取 `request.url.scheme`，`TRUST_PROXY=True` 时改取
   `X-Forwarded-Proto`。不开 `TRUST_PROXY` 时绝不信这个头——攻击者给自己塞个
   `X-Forwarded-Proto: https` 就能骗过 HSTS / 强制 HTTPS 判定。
2. **限流的代理信任跟随全局 `TRUST_PROXY`**（与历史遗留的 `LOGIN_RATE_LIMIT_TRUST_PROXY` 取
   「任一为真」）。后者保留只为兼容既有部署：直接删会让原本开了 XFF 信任的配置静默失效，
   限流键全塌成代理 IP，进而把全站 429。两条开关表达同一件事是刻意保留的兼容层。
3. **`/health` 不参与 HTTPS 跳转**：反代内部通常走 http 探活，跳了反而让探针误判失败。

### 找回密码与修改密码（`app/api/auth.py` + `app/core/verification.py`）

- **找回密码（无需旧密码）**：`POST /auth/password/reset/send` 对**已验证**的邮箱/手机申请验证码，`POST /auth/password/reset/confirm` 凭码设置新密码。面向「忘记密码」以及生产环境被空密码告警拦住的账号——这类用户本来就登不进系统、拿不出旧密码，身份由「用户名 + 控制已验证联系方式（OTP 证明）」承担。
- **投递地址取自库中已验证联系方式**，而非客户端随意填写的 `target`，避免钓鱼/误填；渠道未验证直接 `400`，不发码。
- **与验证用 OTP 用途隔离**：`request_code` / `confirm_code` 新增 `purpose` 参数（默认 `verify`），找回码为 `purpose="reset"`，双方互不通用——找回码不能拿去当验证用，反之亦然（由 `tests/test_password_reset.py::test_reset_code_not_reusable_for_verify` 守住）。
- **登录态自助改密**：`PATCH /auth/me/password` 需提供正确的原密码后才能改密，补上此前**没有任何改密入口**的缺口（`users.update_user` 只允许改昵称/手机/启用状态，并不能改密码；管理员也改不了存量用户的密码）。
- **改密即失效旧令牌（会话失效）**：`User.pwd_changed_at` 记录最后一次改密时间，令牌签发时间 `iat` 早于该值即判失效——访问令牌在 `get_current_user` 拦截、刷新令牌在 `/auth/refresh` 拦截。否则改密/找回踢不掉已泄露的会话（刷新令牌存活 7 天、可无限续期），重置就失去意义。
  - `pwd_changed_at` 为 `NULL`（从未改密）时令牌保持有效，存量数据无需刷数据，向后兼容。
  - 该时间**必须按 UTC 存且截断到整秒**：`iat` 是 `int(time.time())`（UTC 基准、整秒精度），用本地时间存会整体偏移；若带微秒，「改密后同一秒内签发的新令牌」会因 `iat < pwd_ts` 被误杀，把刚登录的用户踢下线。
- **刷新令牌轮转（rotation）+ 重放检测**：每次 `/auth/refresh` 都作废旧刷新令牌、在同一 `family` 链上签发新的一条，因此泄露的令牌**最多只能被用一次**。
  - 轮转**必须有服务端状态**（`refresh_tokens` 表）才有意义：无状态时换发新码而旧码在有效期内依旧可用，那只是「安全假象」，挡不住无限重放——这正是此前代码注释里写明「不做伪轮转」的原因。
  - 已用过的令牌再次出现即判**重放**（多半已泄露），撤销同一 family 的全部令牌，强制重新登录。
  - 登出同样在服务端撤销整条链：只清 Cookie 的话，泄露出去的令牌 7 天内仍可换发访问令牌。
  - 每次登录是一条独立 family，多设备 / 多浏览器并存、互不影响。
  - **定期清理**：轮转只增不减，由 `scripts/cleanup_refresh_tokens.py` 清理（建议每天一次 cron）。它**只删两类安全行**——已过期（`expires_at` 已过）和已撤销且超过保留期（默认 30 天）的；**「已轮换但尚未过期」的行必须保留**，否则重放检测会退化：虽然同样返回 401，却拿不到「撤销整条 family」这一更强的处置。

### 支付渠道路由（`app/core/payment.py` + `app/api/payments.py`）

**配置驱动、渠道可插拔**：`PAYMENT_PROVIDER=mock|wechat|alipay`，商户号 / 密钥路径 / 回调基址
全部来自环境变量，同一套镜像可在「本地联调 / 预发 / 生产」之间切换。

- **非 mock 时凭据缺失启动即报错**（fail-fast）。支付凭据配错（少个商户号、环境变量名打错）
  不会在启动时暴露，只会在**用户真的去付款**那一步失败——那时库存已扣、钱已收，
  表现为「付不了款」或更糟的「付了款订单没更新」，是线上最难查的一类故障。
- **微信只做 Native（扫码）**：JSAPI 需要用户 `openid`，而 openid 只能由公众号 / 小程序授权
  换来，本项目没有微信身份体系；Native 返回 `code_url` 生成二维码即可，不需要用户身份。
- **支付宝用电脑网站支付**（`alipay.trade.page.pay`），前端拿到跳转 URL。只有**异步通知**
  才是可靠的到账依据（同步跳转可被拦截或伪造），推进订单只看异步回调。

**四条铁律**（这段是资金安全，不是代码风格）：

1. **`pay` 事件绝不进 `BUYER_ALLOWED_EVENTS`** —— 那等于允许买家自己把订单标记成已付款
   而不真付钱（0 元提货）。推进只能由渠道回调以 `system:payment-callback` 身份触发，
   审计时间线里一眼能区分「用户自己点的」和「渠道回调推的」。
2. **回调必须验签后才可信**。未验签就采信，等于任何人 POST 一下就能把订单改成已支付。
   微信走 RSA-SHA256 + `resource` 的 AES-GCM 解密，支付宝走 RSA2。
3. **回调必须幂等**。渠道收不到成功响应会阶梯重发数小时，不幂等就会重复推进、重复记流水。
4. **手动动作接口绝不推进 `pay`** —— `POST /orders/{id}/actions/pay` 在非 mock 渠道下
   一律 403，哪怕管理员在订单抽屉里点「支付」也只会被拒，钱没到账就不能把订单
   标成已付款（0 元提货）。`pay` 只由渠道异步回调（验签后）以 `system:payment-callback`
   身份触发；mock 渠道为演示/联调保留手动推进。前端 `available_events` 在非 mock 渠道下
   也会剔除 `pay`，抽屉不再渲染「手动支付」按钮。

回调其余两道防线：**金额比对**（与应收不符一律拒绝——少了说明被篡改，多了说明渠道侧配错，
都不能默默放行）、**找不到流水不凭空建单**。

- 对账字段 `Payment.provider_trade_no` 存渠道侧交易号：出现「用户说付了但订单没更新」时，
  拿这个号去渠道后台查是唯一凭据。
- 支付流水副作用**幂等**：`pay` 的副作用不会重复建流水（有 pending 就置成功、已是 success
  就不动），否则同一订单多条流水会被对账算成收了多次钱——这种错在财务报表上极难发现。
- **无新增后端运行时依赖**：RSA 签名复用已为 MySQL `caching_sha2_password` 引入的 `cryptography`，
  HTTP 复用 `httpx`。前端为渲染微信二维码新增了 `qrcode`（本次唯一新增依赖），见下。
- **前端入口**（`OrdersView.vue` + `api.js`）：订单页「去支付」**仅对本人待付款订单**显示——
  管理员能看到全站订单，但替别人付款会被后端以 404 拒绝，给入口等于把后端错误甩给用户。
  点击走 `POST /orders/{id}/payments`，**不是** `actions/pay`（后者买家必然 403）。
  微信渲染二维码、支付宝新开收银台，两者都**轮询订单状态**等待渠道异步回调的结果
  （上限 40 次 × 3 秒 ≈ 2 分钟，与二维码有效期同量级，超时提示手动刷新，避免无限轮询）。
  二维码库用**动态 import** 拆成独立 chunk：走支付宝 / mock 的用户不会加载它。

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册（直接返回 token） |
| POST | `/api/v1/auth/login` | 登录换取 JWT（**未验证邮箱/手机返回 403**） |
| GET | `/api/v1/auth/me` | 当前登录用户（含 email / email_verified / phone_verified） |
| POST | `/api/v1/auth/logout` | 退出登录（清除 httpOnly Cookie **并在服务端撤销该登录会话的刷新令牌**） |
| POST | `/api/v1/auth/refresh` | 用刷新令牌换发访问令牌（**轮转**：旧刷新令牌随即失效，响应回传新刷新令牌） |
| POST | `/api/v1/auth/verification/send` | 申请邮箱/手机验证码（开发环境回传 dev_code；**支持登录前凭账号密码自证身份**） |
| POST | `/api/v1/auth/verification/confirm` | 确认验证码并标记对应渠道已验证（**未登录凭账号密码自证亦可**） |
| POST | `/api/v1/auth/password/reset/send` | 申请找回密码验证码（**无需旧密码**；发往已验证邮箱/手机） |
| POST | `/api/v1/auth/password/reset/confirm` | 凭验证码重置密码（**无需旧密码**） |
| PATCH | `/api/v1/auth/me/password` | 登录用户修改自己的密码（需提供正确的原密码） |
| GET | `/api/v1/products` | 商品列表（含 SKU） |
| POST | `/api/v1/products` | 创建商品（**仅管理员**；否则买家可自建 `price=0.01` 商品绕开改价闸门） |
| PATCH | `/api/v1/products/{id}/shelf` | 上架 / 下架（**仅管理员**；否则单个买家就能让商城列表空掉） |
| PATCH | `/api/v1/products/{id}` | 编辑商品（名称 / 描述 / 封面 / 分类，**仅管理员**） |
| DELETE | `/api/v1/products/{id}` | 删除商品（仅管理员；级联清理 SKU。购物车或历史订单明细存在引用时拒绝，提示改用下架） |
| PATCH | `/api/v1/products/{id}/skus/{sku_id}` | 调整 SKU 价格 / 库存（**仅管理员**；product_id + sku_id 联合校验防越权改价） |
| POST | `/api/v1/uploads` | 上传图片（**仅管理员**；按文件头魔数校验类型，返回可访问 URL） |
| GET | `/uploads/{filename}` | 读取上传的图片（**不鉴权**：商品封面需游客可见） |
| GET | `/api/v1/stats` | 统计概览（**仅管理员**）：订单数 / 状态分布 / GMV、商品与库存、活跃用户、近 N 天；`low_stock_threshold` 可配 |
| GET | `/api/v1/stats/trend` | 按天统计订单数与销售额（**仅管理员**；看板图用，`days` 1–90） |
| GET | `/api/v1/orders` | 订单列表（管理员见全部，买家仅见自己的订单） |
| POST | `/api/v1/orders` | 创建订单（自动启动工作流，**归属当前用户**；未验证邮箱/手机返回 `403`） |
| GET | `/api/v1/cart` | 我的购物车列表（含合计） |
| POST | `/api/v1/cart` | 加入购物车（同 SKU 自动累加） |
| PATCH | `/api/v1/cart/{id}` | 修改数量（传 0 表示移除） |
| DELETE | `/api/v1/cart/{id}` | 移除商品 |
| POST | `/api/v1/cart/checkout` | 结算购物车（生成订单并清空；**与下单共用验证闸门**，未验证返回 `403`） |
| POST | `/api/v1/orders/{id}/payments` | 发起支付（**仅订单本人**；`mock` 渠道即成功，真实渠道返回拉起收银台所需参数） |
| POST | `/api/v1/payments/notify/{channel}` | 支付渠道异步回调（**不鉴权**，安全性完全靠**验签**；通过后以 system 身份推进订单） |
| GET | `/api/v1/users` | 用户列表（active_only 过滤） |

> **鉴权**：除 `/health` 与 `/auth/login`、`/auth/register`、`/auth/logout`，以及免登录自助入口
> （`/auth/verification/send|confirm`、`/auth/password/reset/send|confirm`）外，所有接口都必须携带身份凭证
> —— 优先 `Authorization: Bearer <token>`（API 客户端 / 测试），浏览器同源请求也可走 `HttpOnly` Cookie。
> 订单归属、购物车、地址都取自令牌中的用户身份，**不再信任请求体/路径里的 `user_id`**，
> 从根上消除冒充他人下单、看他人地址的越权。
| POST | `/api/v1/users` | 创建用户 |
| GET | `/api/v1/users/{id}` | 用户详情（含地址） |
| PATCH | `/api/v1/users/{id}` | 更新用户 |
| DELETE | `/api/v1/users/{id}` | 禁用用户（软删除） |
| GET | `/api/v1/users/{id}/addresses` | 收货地址列表 |
| POST | `/api/v1/users/{id}/addresses` | 新增地址 |
| PATCH | `/api/v1/users/{id}/addresses/{aid}` | 修改地址 |
| DELETE | `/api/v1/users/{id}/addresses/{aid}` | 删除地址 |
| GET | `/api/v1/categories` | 分类列表（带商品数） |
| GET | `/api/v1/categories/tree` | 分类树 |
| POST | `/api/v1/categories` | 创建分类（**仅管理员**） |
| PATCH | `/api/v1/categories/{id}` | 修改分类（**仅管理员**） |
| DELETE | `/api/v1/categories/{id}` | 删除分类（**仅管理员**；有商品时拒绝） |
| GET | `/api/v1/orders/{id}` | 订单详情，含明细、可执行动作、流转时间线 |
| POST | `/api/v1/orders/{id}/actions/{event}` | 推进流转（pay/ship/confirm/cancel/refund/approve/reject）；**按角色分流**：管理员任意事件 / 任意订单，买家仅限本人订单的 `cancel`、`confirm` |
| GET | `/api/v1/workflows/definitions` | 流程定义列表（`?code=` 按业务 code 过滤版本行） |
| GET | `/api/v1/workflows/definitions/{id}` | 流程定义图（设计器加载用） |
| POST | `/api/v1/workflows/definitions` | 创建流程定义（**仅管理员**） |
| PUT | `/api/v1/workflows/definitions/{id}` | 更新流程（全量替换节点与流转边，**仅管理员**） |
| POST | `/api/v1/workflows/definitions/{id}/versions` | 派生新版本（克隆图，version = max+1，**仅管理员**） |
| GET | `/api/v1/workflows/definitions/code/{code}/versions` | 版本历史（按 version 倒序） |
| POST | `/api/v1/workflows/definitions/{id}/publish` | 发布前校验（必须有 start/end、无悬空引用）；发布时降级同 code 其它 published 版本（**仅管理员**） |
| DELETE | `/api/v1/workflows/definitions/{id}` | 归档流程定义（**仅管理员**） |
| GET | `/api/v1/admin/db/tables` | 数据库表浏览（只读，**仅管理员**） |

### 购物车（`backend/app/api/cart.py`）

- 同一 SKU 重复加入**累加数量**而非新增一行，避免同一商品在列表里出现多次
- 累加后仍受库存约束，不能靠反复加入突破上限
- 结算与「清空购物车」在同一事务内完成：下单失败时购物车保留，
  不会出现「订单没生成、购物车却被清空」

### 用户与地址（`backend/app/api/users.py`）

- 删除用户是**软删除**（置 `is_active=False`）：`orders.user_id` 是外键，
  物理删除会破坏历史订单，电商系统里用户数据必须保留
- 地址接口把 `user_id` 放在路径中（`/users/{id}/addresses/{aid}`），
  查询时 `user_id` 与 `address_id` 联合过滤 —— 这是从购物车越权 bug 学到的教训：
  与其事后补校验，不如设计成不容易写错
- **默认地址互斥**：设为默认时自动清除该用户其它地址的默认标记，保证默认地址唯一

### 分类（`backend/app/api/categories.py`）

- 两级树结构（`parent_id`），`/categories/tree` 直接返回带 children 的树
- `products.category_id` 是普通 Integer 而非外键，**数据库不会兜底** ——
  删除分类前必须在应用层校验有无商品/子分类引用，否则商品会指向不存在的分类

### 前端管理后台（`frontend/`）

Vue 3 + Vite 6 + Element Plus，暗色主题。

- **订单管理**：状态筛选、详情抽屉（商品明细 + 流转时间线 + 可执行操作）
- **流程设计器**：SVG 画布支持拖拽节点、增删节点/流转、编辑条件表达式与优先级、保存与发布
- 前端不含任何状态判断——可执行按钮完全由引擎的 `available_events` 决定，
  流程一改按钮自动跟着变，不需要同步修改前端代码

### 工具脚本

| 脚本 | 用途 |
| --- | --- |
| `backend/scripts/init_db.py` | 建库建表，支持 `--dialect sqlite/mysql`、`--drop` |
| `backend/scripts/seed.py` | 灌演示数据，支持 `--reset` |
| `backend/scripts/export_schema.py` | 导出表结构 Markdown |
| `backend/scripts/export_schema_html.py` | 生成表结构可视化页面 `docs/schema-viewer.html` |
| `backend/scripts/export_data_html.py` | 生成数据浏览器页面 `docs/data-viewer.html` |
| `backend/scripts/cleanup_refresh_tokens.py` | 清理 `refresh_tokens` 死记录（**已过期** + **已撤销超保留期**），支持 `--dry-run`、`--revoked-retention-days`（默认 30）；供 cron / 计划任务每天执行 |
| `backend/scripts/cleanup_uploads.py` | 清理**孤儿上传图片**（未被任何 `Product.cover` 引用 **且** 超过保留期），支持 `--dry-run`、`--retention-hours`（默认 24）；供 cron 每天执行 |
| `e2e_acceptance.py` | **端到端验收**：跑在真实 compose 栈上（MySQL + Redis + 生产形态后端），42 项断言覆盖注册/验证/登录、越权、下单主链路、买家自助取消、上传与游客读图、SKU 调库存、刷新令牌轮转与重放、登出。口令从环境变量取，不写死在脚本里 |

定期清理示例（`/auth/refresh` 会持续新增行，必须定期回收）：

```bash
# 先看看会删多少（不真删）
python backend/scripts/cleanup_refresh_tokens.py --dry-run

# Linux cron：每天凌晨 3 点
0 3 * * * cd /path/to/flowmart/backend && python scripts/cleanup_refresh_tokens.py >> /var/log/flowmart-cleanup.log 2>&1
```

孤儿图片清理同理（商品删除 / 换封面后旧图不会自动消失，且对外公开可读）：

```bash
python backend/scripts/cleanup_uploads.py --dry-run
0 4 * * * cd /path/to/flowmart/backend && python scripts/cleanup_uploads.py >> /var/log/flowmart-cleanup.log 2>&1
```

> 保留期**不可省略**：上传与保存表单是两次请求，刚传好的图还没挂到商品上，
> 没有宽限期就会把用户刚上传的封面删掉。

### 静态检查（L0）

在单测之前拦住低级错误：未定义名、未使用导入、拼错变量、超长行、导入未排序。

```bash
# 后端：ruff
pip install -r requirements-dev.txt
ruff check .

# 前端：eslint
cd frontend && npm run lint
```

规则**刻意克制**，只开 `F / E / W / I`（ruff）与 `flat/essential`（eslint）：
老代码一上来就开 B(flake8-bugbear) / C90(复杂度) / N(命名) 会产出几十上百条，
结果要么引发大改、要么整段 noqa / disable——两种结果都比不开更糟。
等基线稳定后再逐项加。

行宽按项目实际基线定为 **120**（中文注释与长校验逻辑较多），
而不是默认 88——设太窄会让 E501 泛滥，最后只能靠 noqa 压下去。
迁移脚本（`backend/migrations/versions/*.py`）整体豁免：它由 autogenerate 生成，
风格不受控，改了下次还会被覆盖回来。

`requirements-dev.txt` 与 `requirements.txt` **分开**：生产镜像只装后者，
不该为一个用不上的 linter 增加体积与攻击面。

### 接口契约快照

`backend/tests/test_openapi_snapshot.py` 把**接口定义本身**当基准，防止后端改了形状而前端不知情。

为什么单测和端到端都补不上这个洞：它们断言的是「我知道该断言什么」，字段改名后我会同步改断言 → 照样绿；
端到端脚本同理。但**前端不是同步改的**：后端把 `order_id` 改成 `id`、或给响应加个必填字段，
前端 `api.js` 还在按旧形状取值，要到运行时才炸，而且往往炸在用户身上。

- 快照：`docs/openapi-snapshot.json`（56 个接口的 path / 参数 / 请求体 / 响应 schema）
- 只快照**形状**：`summary`/`description` 这类文案不纳入，否则快照天天需要重生成，
  最后必然养成 `OPENAPI_UPDATE=1` 无脑刷新的习惯——那这个测试就死了
- 确认变更是有意的之后再更新：

```bash
OPENAPI_UPDATE=1 python -m pytest backend/tests/test_openapi_snapshot.py -q
```

### 端到端验收

功能测试之外，另有一个跑在**真实部署栈**上的验收脚本（不靠 SQLite、不靠开发期开关）：

```bash
cp .env.example .env        # 改好口令
docker compose up -d --build
docker compose run --rm backend python scripts/init_db.py
docker compose run --rm backend python scripts/seed.py --reset

set -a; . ./.env; set +a    # 把 .env 导入环境变量（脚本只从环境取数据库口令）
python e2e_acceptance.py    # 期望：通过 42/42
```

它刻意用**生产形态**跑：`DEBUG=false` 下验证码不会回传给前端，于是脚本从 MySQL 里读出验证码再确认；
演示密码不会回填，于是管理员账号走「注册 → 验证 → SQL 提升」。刷新令牌相关断言走 **Bearer 头**
（端点对 API 客户端的支持路径），因为生产模式 `COOKIE_SECURE=true`，非 HTTPS 下 Cookie 行为不可靠。

> Windows 可用「任务计划程序」按同样命令建每日任务；容器内可用 Kubernetes CronJob / supervisord。
> 未接任何后台调度依赖——本项目坚持无第三方调度组件（连 JWT 与 PBKDF2 都是标准库实现）。

## 内置演示流程

订单主流程 7 节点 9 流转，含一处按金额分流的退款路由：

```
start → submit → 待付款 → pay → 待发货 → ship → 已发货 → confirm → 已完成
                   │                  │
                 cancel           refund（金额 < 1000 → 已关闭）
                   │                  └── refund（金额 ≥ 1000 → 退款审核中）
                   ↓                            ├─ approve → 已关闭
                 已关闭                          └─ reject  → 待发货
```

## 目录结构

```
backend/
  app/
    api/        REST 接口（products / orders / workflows / admin_db）
    core/       配置与数据库连接
    models/     ORM 模型（ecommerce / workflow）
    services/   工作流引擎、订单服务
  scripts/      初始化、种子数据、可视化导出
  tests/        pytest 用例
frontend/
  src/
    views/      OrdersView（订单管理）、DesignerView（流程设计器）
    api.js      接口封装
docs/            表结构与数据可视化页面（由脚本生成）
```

## 部署（Docker）

一条命令起整套环境（backend + MySQL + Redis）。前端构建产物也打进同一个镜像，
由后端在 8000 端口一起提供——**同源部署**：不需要配 CORS，Cookie 的 Secure / SameSite 也不会因跨站失效。

```bash
cp .env.example .env        # 至少改 SECRET_KEY、两个密码，以及验证码投递（邮件 / 短信）
docker compose up -d --build
docker compose run --rm backend python scripts/init_db.py   # 首次建表
docker compose run --rm backend python scripts/seed.py      # 灌演示数据（可选）
```

打开 <http://localhost:8000>。

### 几个刻意的选择

| 选择 | 原因 |
| --- | --- |
| 前后端同镜像、同端口 | 前端用相对路径 `/api/v1`，同源即无需 CORS；少一个 nginx，就少一整套反向代理与容器内 DNS 解析的坑 |
| 上传目录挂卷 | 不挂卷的话容器一删，商品封面全没 |
| MySQL 映射宿主 3308 | 避开本机可能已占用的 3306 |
| Redis 不映射宿主端口 | 限流计数只在 compose 网络内用，避免与本机已装的 Redis（6379）冲突；想用本机 Redis 就把 `LOGIN_RATE_LIMIT_REDIS_URL` 改成 `redis://host.docker.internal:6379/0` |
| 镜像内以非 root 运行 | 即便容器被攻破，拿到的也只是低权限用户 |
| 验证码投递**不给可用默认值** | compose 里 `SMTP_HOST` / `SMS_WEBHOOK_URL` 默认为空，没配好就起不来（启动校验直接拦下）。给个占位地址让容器正常起来、用户却永远收不到码，比起不来更糟——那种故障要到「用户注册到一半」才被发现 |
| TLS 在 LB / 反向代理层终结，镜像内不挂证书、不用 nginx | 生产要求 `COOKIE_SECURE=True`（HTTPS），但 TLS 交给基础设施（云 ALB / Cloudflare / 轻量反代如 Caddy）终结，镜像只跑 8000 裸 HTTP；既满足 HTTPS，又守住「不用 nginx」的取舍，证书续期也由基础设施统一管 |

### Redis 与限流

`LOGIN_RATE_LIMIT_REDIS_URL` 非空时，登录失败限流与通用限流都会改用 Redis 计数（同一套 `RateLimitStore`）。
**多实例部署必须走 Redis**——否则每个容器各计各的，攻击者把请求打散到不同实例就能绕过限流。
单实例留空即可（用进程内内存，零依赖）。

### 生产环境启动校验

`DEBUG=false` 时配置会在**启动时**强制校验，不满足直接报错退出（而不是悄悄裸奔）：

- `SECRET_KEY` 不能是开发默认值（否则任何人都能伪造令牌）
- `COOKIE_SECURE` 必须为 `True`（否则会话 Cookie 经明文 HTTP 泄露）
- `OTP_DEV_RETURN_CODE` 必须为 `False`（否则验证码被明文回传给前端，等于没有验证）
- `BOOTSTRAP_ADMIN` 不能为空（否则谁都不是管理员，系统静默锁死）
- **验证码投递不能有渠道落到 `console`**（`OTP_SENDER` / `OTP_EMAIL_SENDER` / `OTP_SMS_SENDER`）：
  生产用 console 意味着验证码只打进服务端日志——用户永远收不到，而任何能看日志的人
  都能完成任意账号的验证，验证形同虚设。配了 `smtp` / `webhook` 却缺连接参数同样启动报错。
  唯一例外是**端到端验收**：`e2e_acceptance.py` 直接从库里读验证码、没有真实渠道，
  此时显式设 `OTP_ALLOW_CONSOLE_IN_PROD=true`，别的地方一律保持 `False`。
- `DEPLOY_ENV=production` 必须与 `DEBUG=False` 同号：production 是明确的「线上」语义标记，
  绝不能和开着调试（`DEBUG=True`，堆栈 / 文档全开、密钥校验全关）同时出现。

> 本地用 HTTP 调试时若浏览器不接受 Secure Cookie，可临时 `DEBUG=true`，
> 但那会**同时跳过上述全部校验**，仅限开发。

### 生产 TLS：在负载均衡 / 反向代理层终结

镜像里 `uvicorn` 只跑 `0.0.0.0:8000` 的**裸 HTTP**（刻意不在容器内终结 TLS，也**不用 nginx**）。
但生产要求 `COOKIE_SECURE=True`（HTTPS），所以 HTTPS 必须在镜像**之外**的一层终结，再转发
HTTP 给容器：

- **云负载均衡 / CDN**（云 ALB、Cloudflare 等）：在 LB 上挂证书、对外 443，回源用 HTTP 打到容器 8000。
  这类托管服务会顺手做好证书续期、HTTP→HTTPS 重定向、健康检查。
- **自托管轻量反代**（不想引入 nginx 时，用 Caddy 等）：由它监听 443 终结 TLS，反代到 `:8000`。

无论哪种，容器本身始终保持 HTTP，配置上只需告诉应用「前面有人替我终结了 TLS、并诚实转发了
客户端真实信息」：

| 变量 | 生产取值 | 作用 |
| --- | --- | --- |
| `PUBLIC_BASE_URL` | `https://shop.example.com` | 站点公网基址（含 https）；支付回调地址、HTTPS 跳转目标都回退到它，域名只配一次 |
| `PAYMENT_NOTIFY_BASE_URL` | `https://shop.example.com` | 支付/退款回调必须 https，留空则回退 `PUBLIC_BASE_URL` |
| `TRUST_PROXY` | `True` | 信任 LB/反代写来的 `X-Forwarded-For` / `X-Forwarded-Proto`，应用据此判断真实客户端 IP 与协议（限流、HTTPS 判定都依赖它） |
| `COOKIE_SECURE` | `True` | 生产强制（启动已校验），HttpOnly Cookie 仅经 HTTPS 下发 |
| `ENFORCE_HTTPS` | 可选 `True` | 应用层再 307 跳 http→https；**必须同时 `TRUST_PROXY=True`**，否则反代转发的 http 会被无限重定向。多数 LB 自己已做重定向，这层可不开 |
| `COOKIE_DOMAIN` | 子域分流时设 `.example.com` | 前后端分处不同子域时让令牌 Cookie 跨子域生效；同源部署留空即可 |

> **健康检查**：LB 通常用 HTTP 探活，`/health` 刻意**不参与 HTTPS 跳转**，探活直接打
> `http://<容器>:8000/health` 即可，不会被 307 绊住。

自托管（Caddy）的最小反代示例（非 nginx，契合「不用 nginx」取舍）：

```caddyfile
shop.example.com {
    encode gzip
    reverse_proxy 127.0.0.1:8000
}
```

证书由 Caddy 自动申请（ACME）与续期，无需手动管理。换成云 LB 时这段直接删掉、改为在控制台挂证书——
应用侧配置完全不变。

### CI

`.github/workflows/ci.yml` 跑三件事：后端 pytest、前端 `npm ci && npm run build`、部署镜像构建。

## MySQL 兼容性验证

开发默认用 SQLite，但生产目标是 MySQL。已在 **MySQL 8.0.45** 上完成实跑验证：

| 验证项 | 结果 |
| --- | --- |
| 建库建表 | 14 张表全部创建成功 |
| 种子数据 | 7 个订单、25 条流转日志，退款按金额分流正确 |
| 下单 | 库存扣减、金额计算（Decimal）正确 |
| 流转推进 | 含 approve 等设计器新增的事件 |
| 购物车 | 加购 / 累加 / 结算 / 清空全链路正常 |
| 流程设计器 | 保存（全量替换节点与流转）正常 |

针对方言差异已做的处理：

- `Order.status` 长度对齐 `wf_nodes.key`（MySQL strict 模式超长会直接报错，SQLite 却静默放行）
- SQL 标识符引用按方言区分：MySQL 用反引号、SQLite 用双引号

切换到 MySQL：`DB_DIALECT=mysql` 环境变量，或写入 `.env`。

## 环境注意事项（Windows 踩坑记录）

- **pip 走代理会失败**：本环境设置了 `HTTPS_PROXY`，访问清华源报 `No matching distribution found`，
  改用官方源即可正常安装。
- **Vite 默认只监听 IPv6**：在 `vite.config.js` 里显式配置 `host: '127.0.0.1'`，
  否则 `127.0.0.1:5173` 连不上。
- **pnpm 会拦截第三方构建脚本**：导致 esbuild 安装不完整、Vite 起不来。
  本项目改用 npm，原因记录在 `frontend/.npmrc`。
- **Git Bash 下 `taskkill /F` 参数会被路径转换**：需 `export MSYS_NO_PATHCONV=1`。

## 功能清单（状态看板）

> 详细设计见上方「已完成功能」。此处为功能级勾选，便于一眼看清进度。
> 清单内功能**已全部落地，暂无待实现项**（新增功能请直接追加到下方列表）。

### ✅ 已实现

- [x] 工作流引擎（数据库驱动、可配置：`start/fire/available_events`、流转日志 + 上下文快照、非法流转显式报错）
- [x] 订单服务（创建订单同事务：校验库存 → 算金额 → 扣库存 → 启动流程 → 推进待付款）
- [x] 库存并发控制（DB 层原子条件 UPDATE：`UPDATE ... WHERE stock >= qty` 靠 `rowcount==0` 判不足；归还用 `stock = stock + qty` 累加，杜绝 TOCTOU 超卖与并发丢失更新）
- [x] 鉴权基础（JWT HS256 + PBKDF2 密码哈希 + 依赖注入身份，前端无法伪造 `user_id`）
- [x] 跨域收紧 + JWT 写入 httpOnly Cookie（CORS 仅放行 `settings.CORS_ORIGINS`；`POST /auth/logout` 清 Cookie）
- [x] RBAC 角色权限（管理员 / 普通买家；用户管理仅管理员；订单流转按角色分流——管理员任意事件 / 任意订单，买家仅限本人订单的白名单事件；买家越权访问统一 404；初始管理员可配置且空值启动报错）
- [x] REST API 全量（auth / products / orders / cart / users+addresses / categories / workflows / admin_db 只读）
- [x] 购物车（同 SKU 累加、累加受库存约束、结算与清空同事务）
- [x] 用户与地址（软删除、路径内 `user_id+address_id` 联合过滤防越权、默认地址互斥）
- [x] 分类（两级树；删除前应用层校验商品/子分类引用，防间接环）
- [x] 前端管理后台（订单管理页、流程设计器、登录页）
- [x] 新建订单可选收货地址（OrdersView 弹窗下拉复用 `/users/{id}/addresses`，按令牌归属拉取；选中才传 `address_id` 补全订单 `address_snapshot`，不选中则订单无快照）+ 后端 `create_order` 地址归属校验防 IDOR（他人 `address_id` 与「不存在」同等处理，统一 404 不泄露是否存在）
- [x] 工具脚本（init_db / seed / export_schema / export_data_html / cleanup_refresh_tokens 定期清理 / cleanup_uploads 孤儿图片清理 / expire_unpaid_orders 超时未支付自动取消并归还库存）
- [x] MySQL 8.0.45 实跑验证（建表 / 种子 / 下单 / 流转 / 购物车 / 设计器全链路；方言差异已处理）
- [x] 购物车前端页面（`CartView.vue`：选 SKU 加购、改数量、移除、合计、选地址结算；数量改动受控渲染，失败回滚到后端真实值）
- [x] 商品管理页面（`ProductsView.vue`：搜索 / 状态筛选含下架、SKU 展开明细、上架下架、新建商品含动态 SKU 行）
- [x] 分类管理页面（`CategoriesView.vue`：树形层级、商品数、新建/编辑/删除、加子分类；有子分类或仍被商品引用时后端拒绝删除）
- [x] 用户管理页面（`UsersView.vue`：用户列表/新建/改资料/软禁用 + 本人收货地址增删改；禁用为软删除且不能禁用当前管理员）
- [x] Alembic 迁移脚本（初始迁移已生成并与模型一致；`alembic upgrade head` / `downgrade base`；`tests/test_migrations.py` 守住「改模型忘写迁移」）
- [x] 流程定义版本管理（同 code 多版本；POST .../versions 派生新草案克隆图、GET .../code/{code}/versions 版本历史；发布保证唯一 published 并自动降级旧版本；回滚=重新发布旧版本）
- [x] JWT 刷新 / 续期机制（短期访问令牌 30 分钟 + 长期刷新令牌 7 天写独立 httpOnly Cookie；POST /auth/refresh 静默换发访问令牌；前端 401 自动刷新并重试一次；access/refresh 令牌 type 隔离防混用）
- [x] 刷新令牌轮转（rotation）+ 重放检测（每条刷新令牌在 `refresh_tokens` 表留痕：用过后置 `used_at`，再次出现即判重放并撤销同一 family 整条轮转链；登出在服务端撤销、不只清 Cookie；多设备各是一条独立 family 互不影响）
- [x] 刷新令牌定期清理（`scripts/cleanup_refresh_tokens.py`：只删「已过期」与「已撤销超保留期」两类安全行，**保留已轮换但未过期的行**以免重放检测退化；`--dry-run` 可预演，无第三方调度依赖，挂 cron 即可）
- [x] 列表接口分页（orders / products / users 统一返回 `{items, total}` 信封；`limit=0` 表示不分页返回全部，保证 SKU 下拉框全量不被截断；total 用子查询统计；前端 OrdersView/ProductsView/UsersView 均加 `el-pagination`）
- [x] 登录限流（POST /auth/login 按 (IP, 用户名) 固定窗口计数失败次数，超阈值返 429 + Retry-After；成功清空计数；单实例内存级，生产换 Redis）
- [x] 订单搜索 / 筛选增强（列表支持关键词：订单号 + 商品行项名称 LIKE；下单时间范围 `created_from`/`created_to` 闭区间；非法日期 400；与 status/分页共用同一过滤条件统计 total）
- [x] 邮箱 / 手机验证（两步式 OTP：send/confirm；可插拔发送器；DB 重发限流；开发回传 dev_code；**下单与登录均加 403 强约束闸门**；验证入口支持未登录凭账号密码自助验证，避免死锁）
- [x] 找回密码与改密码（reset 两步式：已验证邮箱/手机收码 → 凭码重置，**无需旧密码**；投递地址取库中已验证联系方式；`purpose` 隔离防找回码与验证码跨用途复用；登录态 `PATCH /auth/me/password` 凭原密码自助改密，补上此前**无任何改密入口**的缺口）
- [x] 改密即失效旧令牌（会话失效）：`User.pwd_changed_at`（UTC、截断到整秒）记录最后改密时间，`iat` 早于它即判失效，访问令牌与刷新令牌一并拦截；`NULL` 视为从未改密，存量数据向后兼容无需刷数据
- [x] init_db 演示密码回填仅限开发环境（`_backfill_demo_passwords` 仅在 `DEBUG=True` 生效；生产 `DEBUG=False` 跳过回填仅告警，绝不静默赋已知明文密码）
- [x] 买家侧订单动作（取消订单 / 确认收货）：推进流转端点由「仅管理员」改为**按角色分流**——管理员保持全能（任意订单 / 任意事件），买家可对自己订单触发白名单事件 `BUYER_ALLOWED_EVENTS = {cancel, confirm}`（**默认拒绝**：未登记事件即使流程定义允许也触发不了）；他人订单越权仍统一 404 不泄露存在性；`operator` 一律取认证身份、不信任请求体，杜绝伪造操作人污染 `wf_transition_logs` 审计轨迹（此前服务层 `OrderService.cancel()` 默认 `operator="user"` 表明取消本就按买家自助设计，但 API 层没暴露，属「服务层有、接口层没接通」的断层）
- [x] 工作流推进并发安全（防重复触发）：`fire()` 用「条件 UPDATE + rowcount」**原子认领**推进（`WHERE current_node_key=:from AND status='running'`），后到的并发请求必然匹配不到行而显式失败，杜绝两个并发 cancel/refund 都执行归还库存导致**库存凭空变多**；副作用从「fire 之前」挪到「fire 成功之后」，失败路径上副作用根本没发生过、不依赖回滚兜底；`available_events` 对已结束实例返回 `[]`，与 fire 的状态口径对齐。另修正一个 MySQL 陷阱：`rowcount` 是「变更行数」而非「匹配行数」，自环流转（`from == to`）会误报并发冲突，故认领失败时回读一次以区分「无人抢先只是没变更」与「真被并发推进」（SQLite 返回匹配数，**此差异单测跑不出来**，只有生产 MySQL 会暴露）
- [x] 商品管理与 SKU 调整（新增 `PATCH /products/{id}` 编辑名称/描述/封面/分类、`PATCH /products/{id}/skus/{sku_id}` 改价与调库存、`DELETE /products/{id}` 删除并级联清理 SKU——此前只有创建与上下架，**改不了也删不掉**，改价调库存更是没有入口。所有商品写操作统一 `require_admin`，**并顺带收紧了既有的创建与上下架**：原先二者只要求登录，买家可自建 `price=0.01 / stock=99999` 的商品达到与改价完全相同的效果（使新增闸门形同虚设），也可一键把全站商品下架让商城列表空掉。调库存区分 `stock`（绝对值盘点修正，语义是「以我为准」、会覆盖并发扣减）与 `stock_delta`（相对量补货，`UPDATE ... SET stock = stock + delta` 原子，不丢更新）。删除沿用分类删除套路：**先校验引用**，购物车（对 skus.id 有外键）或历史订单明细（已售凭证）存在时拒绝硬删并提示改用下架；并发窗口内被加购时兜 `IntegrityError` 为 400 而非 500）
- [x] 前端角色 gate（`/auth/me` 补 `is_admin`；前端据此隐藏买家无权入口：**用户管理**整块 tab、商品页「新建商品」按钮与「操作（上下架）」整列。角色只决定**显示**，真正的校验仍在后端——此前前端零角色判断，收紧商品写操作后买家点每个按钮都会撞 403，等于把后端错误甩给用户）
- [x] 分类 / 流程定义 / admin_db 写操作收紧为管理员（此前**只要求登录**：任意买家可删分类、灌垃圾节点、把在售商品归类清空，更能 `PUT /workflows/definitions/{id}` + `publish` **改写并发布订单状态机**——决定订单往哪流转，严重性高于商品下架；`admin_db.tables` 原本还能枚举库表名。前端同步隐藏「分类管理」「流程设计器」整块 tab，并让 `active` 在角色不足时兜回订单页）
- [x] 通用限流（注册 / 发验证码 / 找回密码 / 登录 per-IP / OTP 确认 / **下单与结算按 user_id**）：登录失败限流只记**失败**、按 (IP, 用户名)，**换用户名就能重置配额**；而这些端点不看成败只看调用量——**成功调用同样消耗配额**，否则可用正确参数高频批量灌账号、把短信/邮件渠道打爆（有成本且骚扰他人）。登录 per-IP 那层尤其关键：每次密码校验都要跑 PBKDF2（几十毫秒 CPU），「用户名 × N 次」就是 CPU 放大 DoS 与凭证填充的通道。OTP 确认那层按 (IP, 用户名) 记每次请求——每条码自身的 `OTP_CONFIRM_MAX_ATTEMPTS` 会被「重新发码」重置，光靠它挡不住稳态猜码（6 位码 + 10 分钟 TTL）。实现上：复用同一套 `RateLimitStore`（内存 / Redis 可切）与同一套 IP 信任策略（默认 socket 地址，XFF 需显式开 `TRUST_PROXY`），避免两处对「客户端是谁」判断不一致而被绕过；两套限流的键加 `login:` / `gen:` 命名空间隔离，并**共用同一个 store 单例**——否则 `reset_all()` 在 Redis 下清全场、内存下只清自己（同操作两种后端行为不一致，开发环境还复现不出来）。刻意做成**按端点 opt-in** 而非全局中间件：一刀切会误伤高频只读接口，也会让测试因「请求太多」随机失败。阈值按 scope 在**请求时**现读 settings（可 monkeypatch），不是装饰器期固化。与 DB 层「同用户重发限流」「每条码尝试上限」互补：那两个防单用户刷自己 / 单条码被猜，这几个防单 IP 刷一堆账号与稳态猜码。**下单 / 结算是唯一按 user_id 而非 IP 的**：已认证端点有稳定身份，按 IP 会在 NAT / CGNAT 下误伤一片正常用户、在 IPv6 / 代理下又形同虚设；而 `create_order` 会原子扣库存，刷单能把库存打到 0，是业务型 DoS 而非资源型
- [x] 图片上传（`POST /api/v1/uploads` 仅管理员；`GET /uploads/{filename}` **不鉴权**，供游客看商品封面）：按「上传口子不能变成任意文件写入跳板」设计——**类型按文件头魔数判定而非信任 Content-Type**（否则一段 HTML 标成 `image/png` 就能存进去，是存储型 XSS 入口）；**文件名自己随机生成、客户端文件名完全不参与**（否则 `../../` 可路径穿越）；限大小（默认 2MB，多读 1 字节判定超限，不必把超大文件整体读进内存）；上传目录已加 `.gitignore`。存本地磁盘 + 静态挂载，换 OSS/S3 只需替换写入逻辑，对外返回的 URL 形状不变。加固项：①**按 Content-Length 在接收前就拦一道**——endpoint 拿到的 file 已是「整个请求体接收并落临时文件之后」的结果，光靠 `read(MAX+1)` 只限制「最终存多少」、限制不了「服务器先收了多少」，磁盘 DoS 得在门口挡；②**魔数之外再查结构**（PNG 须 IHDR 起 / IEND 收、JPEG 须 EOI 收），否则「图片头 + 任意 trailer」会被永久存下并对外可读，等于把安全性押在「下游 Content-Type 永远正确」这个不可验证的假设上；③**原子写**（先 `.part` 再 rename）+ `OSError → 507`，避免写一半失败留下会被 `/uploads` 正常提供的永久坏图；④**启动时校验 `UPLOAD_DIR` 不能指向项目根 / backend**——配错会把 `.env`、源码、数据库在**无任何告警**的情况下匿名公开；⑤响应加 `X-Content-Type-Options: nosniff` 兜底；⑥静态挂载显式 `follow_symlink=False`（改 True 会使穿越防护失效）。**生命周期与配额**：`scripts/cleanup_uploads.py` 清理孤儿图片（未被引**且**超过 24h 保留期——保留期不可省，上传与保存表单是两次请求，否则会删掉用户刚传好还没保存的封面）；配额两层——按 **user_id** 的速率上限（刷上传是最直接的磁盘 DoS）与**总量上限**（默认 512MB，单文件上限挡不住「慢慢攒满磁盘」）
- [x] 统计看板（后端 `GET /api/v1/stats` 概览 + `/stats/trend` 按天趋势，前端 `StatsView.vue` 指标卡 + 状态分布 + 趋势表，**仅管理员**；刻意**不引入图表库**，Element Plus 无图表，为几张图背一个 echarts 不划算，指标卡 + 表格已经够看）：订单总数 / 状态分布 / **GMV**、商品与 SKU 数 / 库存总量 / 低库存数、活跃用户数、近 N 天订单与销售额。两个刻意的设计：①**只做 SQL 聚合、不把明细读进内存**——订单与商品行数会随时间增长，在 Python 里 `sum()` 迟早拖垮接口；②**GMV 口径是「已支付订单的 pay_amount 之和」**，未支付订单计入订单数但不计入销售额（这条最容易搞错，有专门测试钉死）。低库存阈值做成参数而非写死——不同品类「缺货」标准不同（手机 3 台算紧张、数据线 30 条可能不算）
- [x] 部署配套（Dockerfile 多阶段构建 + docker-compose + GitHub Actions CI）：前端构建产物打进同一镜像、由后端在 8000 端口一起提供，**同源部署**——前端用相对路径 `/api/v1`，同源即无需 CORS，Cookie 的 Secure/SameSite 也不会因跨站失效，还省掉一整套 nginx 反代与容器内 DNS 解析的坑。镜像以**非 root** 运行并带 HEALTHCHECK；上传目录挂卷（否则容器一删封面全没）；MySQL 映射宿主 3308 避开本机 3306、Redis 不映射宿主端口避免与本机 Redis 冲突。`LOGIN_RATE_LIMIT_REDIS_URL` 默认指向 compose 内的 redis，让限流计数跨实例共享（否则每容器各计各的，把请求打散到不同实例就能绕过限流）。**踩坑**：前端 `Mount("/")` 会按前缀吞掉 `/health`，健康检查必须注册在前端挂载之前，否则 HEALTHCHECK 永远失败、容器被判不健康
- [x] 静态检查 L0（后端 ruff + 前端 eslint，均接进 CI）：规则**刻意克制**——ruff 只开 `F/E/W/I`、eslint 用 `flat/essential`，老代码一上来开全量规则会产出几十上百条，结果要么大改、要么整段 noqa，两种都比不开更糟。行宽按项目实际基线定 120（非默认 88），否则 E501 泛滥只能靠 noqa 压下去。迁移脚本整体豁免（autogenerate 生成，改了会被覆盖回来）。`requirements-dev.txt` 与生产依赖分开，镜像不装 linter
- [x] 接口契约快照（`docs/openapi-snapshot.json`，56 个接口）：把**接口定义本身**当基准。单测与端到端都补不上这个洞——它们断言的是「我知道该断言什么」，字段改名后我会同步改断言，照样绿；但前端不是同步改的，要到运行时才炸。只快照**形状**（path / 参数 / 请求体 / 响应 schema），文案类字段不纳入，否则快照会被无脑刷新、测试等于失效。已验证它真能抓到变更（模拟移除统计接口 → 报错并列出差异）
- [x] 支付渠道路由（配置驱动可插拔 `PAYMENT_PROVIDER=mock|wechat|alipay`：微信 Native 扫码 / 支付宝电脑网站支付，商户号与密钥全走环境变量且**非 mock 时缺失启动即报错**；**`pay` 事件不进买家白名单**、只由渠道回调以 `system:payment-callback` 身份触发，杜绝「自己把订单标记成已付」的 0 元提货——此前买家下单后只能取消、根本付不了款，购买闭环是断的；回调四道防线：验签（微信 RSA-SHA256 + AES-GCM 解密 / 支付宝 RSA2）、幂等（渠道阶梯重发数小时）、金额比对、无流水不建单；`provider_trade_no` 存渠道交易号供对账；支付流水副作用幂等防同一订单重复记账；复用既有 `cryptography` 与 `httpx`，**零新增依赖**）
- [x] 前端支付入口（订单页「去支付」**仅本人待付款订单**可见——管理员可见全站订单但替付会被后端 404 拒绝；点击走 `/payments` 而非 `actions/pay`，后者买家必然 403；微信渲染二维码、支付宝跳收银台，两者均**轮询订单状态**等待渠道异步回调，上限约 2 分钟与二维码有效期同量级；二维码库 `qrcode` 用**动态 import** 拆为独立 chunk，非微信渠道不加载——这是本项目第一个前端新增依赖，理由是微信扫码属硬功能需求）
- [x] 通知渠道可配置（`OTP_SENDER` 全局 + `OTP_EMAIL_SENDER` / `OTP_SMS_SENDER` 分渠道：邮件走标准库 `smtplib` **零新增依赖**、短信走**通用 HTTP 网关**（`{target}/{code}/{ttl}` 占位符模板，可指向厂商也可指向自建转发服务），刻意**不内置任何厂商 SDK**——开源项目不替使用者选厂商；默认 `console` 保证 clone 下来**零凭据**可跑通注册验证，但生产启动校验拒绝任何渠道落到 console；外呼一律带超时，发送失败整段回滚不占重发配额，上游异常统一收敛为 502）
- [x] 域名 / 部署环境 / TLS 可配置（`PUBLIC_BASE_URL` 单一事实来源：支付回调地址留空时回退到它，域名只配一次；`DEPLOY_ENV` 标签进 `/health` 且与 `DEBUG` 强一致；`COOKIE_DOMAIN` 支持前后端分域部署；全局 `TRUST_PROXY` 信任 `X-Forwarded-For/Proto`，限流同时跟随该开关；`ENFORCE_HTTPS` 强制跳转 + `HSTS_MAX_AGE` 发 `Strict-Transport-Security`，两者均配错即启动报错，且协议判定区分直连与反代避免被伪造 `X-Forwarded-Proto` 骗过）
- [x] 支付资金漏洞修复（手动 `pay` 闸门）：`POST /orders/{id}/actions/pay` 在非 mock 渠道下强制 403——管理员在订单抽屉里点「支付」也标不了已付款（堵住「0 元提货」）；`pay` 只由渠道异步回调以 `system:payment-callback` 身份触发。同时 `available_events` 在非 mock 渠道下剔除 `pay`，前端抽屉不再渲染「手动支付」按钮（mock 渠道保留以便调试）。真实支付链路（`create_payment` 发起 / `payment_notify` 回调）不受影响，二者走独立路径、不经该手动接口
- [x] 待付款订单超时回收（库存防泄漏）：新增 `scripts/expire_unpaid_orders.py`，周期（cron）取消超过 `ORDER_PAY_TIMEOUT_MINUTES`（默认 30）分钟仍未支付的订单并原子归还库存，未支付流水标记 `expired` 避免对账把死流水当待支付。工作流引擎无定时事件能力，本脚本即「外部定时器」；只筛 `pending_payment` 且到点未付的订单，已取消/已支付自然不在范围，故**天然幂等**。配置 `ORDER_PAY_TIMEOUT_MINUTES<=0` 表示不启用。`--dry-run` 可预演
- [x] 下单幂等（`idempotency_key`）：防重复提交（双击 / 网络重试 / 弱网卡顿）导致**重复建单 + 重复扣库存**。`Order` 新增全局唯一 `idempotency_key` 列（含 Alembic 迁移），`OrderService.create_order` 接受可选 `idempotency_key`：① 入库前按 `(key, user_id)` 预查，命中即返回已有订单、不重复扣库存不重复启动流程；② 并发竞态下若两条都越过预查，后插入者触发唯一约束冲突（`IntegrityError`），在 `flush` 处捕获后回滚本次扣库存并返回已存在的订单，最终**只建一单、库存只扣一次**；③ 不传 key 或不同 key 各自新建，向后兼容。`POST /api/v1/orders` 的 `OrderCreateIn` 透传该字段并在响应中回显。NULL 列不触发唯一约束冲突（SQLite/PostgreSQL 同语义），旧订单 / 不传 key 的调用方零影响
- [x] 支付回调加固：① **未知 `out_trade_no` → 回成功停止重发**：验签通过且渠道声明支付成功、却查不到对应流水时，原实现回 404 会让微信/支付宝按 15s 阶梯反复重发数小时（纯浪费请求量、还淹没对账噪声）；改为回渠道成功响应让重发立即停止，同时打 `warning` 日志供按 `provider_trade_no` 对账排查（绝不为「凑响应」凭空建单，否则违反「钱到账才推进」铁律）。② **回调入口按 IP 限流**：`/payments/notify/{channel}` 在「验签」之前就套 `rate_limit("payment_callback")`，阈值 `RATE_LIMIT_PAYMENT_CALLBACK_MAX`（默认 60/窗口，可按大促调大、设 0 关闭）；验签是 RSA/AES-GCM 这类 CPU 密集操作，不挡住的话伪造签名的洪水请求就是现成的 CPU 放大 DoS。复用既有 `RateLimitStore`（内存/Redis 可切），IP 取值与全局 `TRUST_PROXY` 一致，避免与登录限流对「客户端是谁」判断不一致而被绕过
- [x] 退款接真实渠道 API：`refund` 事件（大额转人工审核 `refunding`、小额自动关单 `closed`）现在真正把钱退回去，而不是只改本地状态。`PaymentProvider` 新增 `refund()` 抽象方法，三家渠道各自实现：`MockPaymentProvider` 直接回成功；`WechatPayProvider` 走 APIv3 退款接口（`/v3/refund/domestic/transactions/refunds`，用 `transaction_id`=渠道交易号、`out_refund_no` 幂等、APIv3 签名）；`AlipayProvider` 走 `alipay.trade.refund`（RSA2 签名，**且验签响应用支付宝公钥**防伪造「退款成功」）。失败路径：`_effect_mark_refunded` 先调渠道、成功才把流水标记 `refunded`；渠道抛 `PaymentError`（网络/渠道拒绝/验签不过）则整笔回滚（订单回到 `paid`、`refund` 仍可重试），`fire_event` 转 502，绝不会出现「钱没退、订单却关了」。流水新增 `out_refund_no`（我方幂等单号，重试复用）/ `refund_channel_no`（渠道退款单号）两列供对账，含 Alembic 迁移。测试 `test_refund_channel.py` 覆盖 mock 端到端、微信/支付宝签名请求+解析响应、渠道失败保持订单 paid、以及 `out_refund_no` 幂等复用
- [x] 生产 TLS 在负载均衡 / 反向代理层终结（零代码改动）：镜像内始终跑裸 HTTP 8000 端口、不挂证书、不内置 nginx；TLS 解密交给云 LB（ALB/CLB）/ Cloudflare / Caddy 反代，反向代理 `127.0.0.1:8000` 即可。配套把所有「协议从哪来」的判定收敛到一个事实来源：`PUBLIC_BASE_URL`（支付回调地址留空时回退它）、`TRUST_PROXY`（信任 `X-Forwarded-Proto` 判定真实协议）、`ENFORCE_HTTPS`（强制跳转 + `Strict-Transport-Security`）、`COOKIE_SECURE`（Secure 标记）、`COOKIE_DOMAIN`（前后端分域部署）。`DEPLOY_ENV` 进 `/health`、配错即启动报错。已附 Caddy `reverse_proxy 127.0.0.1:8000` 样例与逐项环境变量说明，刻意不在仓库里放证书也不引入 nginx 依赖
- [x] 测试（pytest 全量 338 passed；前端 Vitest 13 条）
