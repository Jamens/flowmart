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
  令牌只含 `sub`（用户 id）、`iat`、`exp`，用 HMAC-SHA256 签名，`hmac.compare_digest` 常量时间校验防时序攻击。
- **令牌存储改为 httpOnly Cookie（抗 XSS）**：登录/注册在返回 Bearer 令牌（供 API 客户端）的同时，
  把 JWT 写入 `HttpOnly` Cookie；浏览器同源请求由 Cookie 自动携带（`SameSite=Lax`），前端不再用 `localStorage`
  存明文令牌，从根上杜绝 XSS 脚本窃取令牌。新增 `POST /auth/logout` 由后端下发删除指令清除 Cookie
  （JS 无法删除 HttpOnly Cookie）。`get_current_user` 优先取 Bearer 头、其次取 Cookie。
- **密码哈希**：PBKDF2-HMAC-SHA256（10 万次迭代）+ 随机盐，存储格式 `pbkdf2$sha256$<iter>$<salt>$<dk>`，
  明文绝不下库；纯标准库实现，无第三方加密依赖。
- **依赖注入取身份**：`get_current_user` 解析令牌返回用户对象，所有资源接口 `Depends` 它，
  因此「当前用户」恒来自令牌，前端无法伪造 `user_id` 冒充他人。
  - 前端下单（`OrdersView.vue` 的 `submitCreate`）只传 `items`，不再带 `user_id`/`address_id` 死字段；即便误带后者后端也忽略，保持归属来源唯一。不传 `address_id` 时订单无收货快照（详情页优雅显示「-」）。
- 登录失败（用户不存在 / 密码错误）统一返回 `401 用户名或密码错误`，不泄露哪些用户名已注册。
- **生产必须设置 `SECRET_KEY`**：`config.SECRET_KEY` 仍为开发默认值时，非 debug 模式启动会直接报错，杜绝「 anyone can forge token 」。
- **RBAC 角色权限（已落地）**：角色分「管理员 / 普通买家」，`User.is_admin` 字段 + `require_admin` 依赖闸门。
  - 用户管理（建/列/禁用账号）与订单流转推进 **仅管理员** 可执行；
  - 普通买家只能改**自己**的资料、只看**自己**的订单；越权访问他人资源统一 `404`，不泄露目标是否存在；
  - 购物车、订单创建、地址归属始终严格取自令牌用户；`GET /users/{id}` 不返回收货地址，地址须经归属校验的 `/users/{id}/addresses` 获取。
  - **初始管理员可配置**：`config.BOOTSTRAP_ADMIN`（默认 `zhangsan`，可经环境变量覆盖）指定首次 `seed` / `init_db` 时提升为管理员的账号；`BOOTSTRAP_ADMIN` 为空会在启动时直接报错，避免「谁都不是管理员」导致系统静默锁死。生产务必改成真实管理员账号。
  - **登录限流（防暴力破解）**：`POST /auth/login` 按 `(客户端IP, 用户名)` 固定窗口计数失败次数，窗口内（`LOGIN_RATE_LIMIT_WINDOW`，默认 60 秒）失败超 `LOGIN_RATE_LIMIT_MAX`（默认 5 次）即返回 `429` 并带 `Retry-After` 头；登录成功清空计数，避免正常用户被旧失败数误伤；禁用账号不计失败次数。
    - **可插拔存储**：默认进程内内存（`MemoryStore`，单实例够用、零依赖）；多实例 / 负载均衡设 `LOGIN_RATE_LIMIT_REDIS_URL`（如 `redis://127.0.0.1:6379/0`）即切 Redis 后端（`INCR+EXPIRE` 原子计数，限流对全部实例统一生效），连不上启动时直接报错（fail-fast）。后端接口不变（`app/core/ratelimit.py`）。
    - **客户端 IP 安全默认**：默认 `LOGIN_RATE_LIMIT_TRUST_PROXY=False`，取 `request.client.host`（直连真实 socket 地址、无法伪造）；仅当反向代理已用真实客户端 IP 覆写 `X-Forwarded-For` 且显式开 `True` 时才信 XFF 首跳——否则攻击者可伪造不同 XFF 绕过限流。

### 邮箱 / 手机验证（`app/core/verification.py` + `app/api/auth.py`）

- 两步式：`POST /auth/verification/send` 申请一次性 OTP，`POST /auth/verification/confirm` 确认；确认成功后把 target 绑定到账号并置对应渠道的 `*_verified=True`。
- 验证码用 `secrets` 生成（密码学随机，非 `random`）；确认用 `hmac.compare_digest` 常量时间比对，防时序侧信道。
- **防爆破**：单个验证码的确认尝试超过 `OTP_CONFIRM_MAX_ATTEMPTS`（默认 5）即锁定该码（置 `consumed_at`），避免对 6 位码暴力枚举；重发本身另有窗口限流。
- **可插拔发送器**：`VerificationSender` ABC + `ConsoleSender`（开发期打印到日志）；`smtp` / `sms` 是预留扩展点，未实现时配了会启动即报错（fail-fast）。
- **重发限流基于 DB**（同一用户对同一渠道在时间窗内最多 N 次），天然多实例安全，不依赖进程内内存或 Redis；每次申请都会作废同渠道同 target 的未消费旧码，防重放。
- **开发便利开关**：`OTP_DEV_RETURN_CODE=True`（默认）时 `send` 接口在响应里回传 `dev_code`，便于联调与测试；生产**必须 False**，配置校验会强制（`DEBUG=False` 下仍为 True 即启动报错）。
- 模型新增 `User.email` / `email_verified` / `phone_verified` 三列，并新建 `verification_codes` 表；已生成 Alembic 迁移（与 `alembic check` 守卫一致）。注册接口支持可选 `email`。
- **下单强约束（已落地）**：`POST /api/v1/orders` 与 `POST /api/v1/cart/checkout` **共用同一依赖 `require_verified_contact`**（单一事实来源，杜绝绕过），在 API 层校验「当前用户已验证邮箱或手机**至少其一**」，否则返回 `403`（detail 指引先走 send/confirm）。**服务层 `OrderService.create_order` 不受限**——后台运营/迁移等直接调用路径不应被账户合规约束拦截；验证状态取运行时实时值，撤销验证后会被重新拦截。
- **只锁下单、不锁登录**：当前强约束仅作用于下单。登录仍不强制验证，避免把全部存量未验证用户（含初始管理员 `BOOTSTRAP_ADMIN`）直接锁死、破坏现有流程。若以后要「未验证不能登录」等更强约束，同理在 `auth.login` 按 `*_verified` 加闸门即可（已预留落点）。

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/auth/register` | 注册（直接返回 token） |
| POST | `/api/v1/auth/login` | 登录换取 JWT |
| GET | `/api/v1/auth/me` | 当前登录用户（含 email / email_verified / phone_verified） |
| POST | `/api/v1/auth/logout` | 退出登录（清除 httpOnly Cookie） |
| POST | `/api/v1/auth/verification/send` | 申请邮箱/手机验证码（开发环境回传 dev_code） |
| POST | `/api/v1/auth/verification/confirm` | 确认验证码并标记对应渠道已验证 |
| GET | `/api/v1/products` | 商品列表（含 SKU） |
| POST | `/api/v1/products` | 创建商品 |
| GET | `/api/v1/orders` | 订单列表（管理员见全部，买家仅见自己的订单） |
| POST | `/api/v1/orders` | 创建订单（自动启动工作流，**归属当前用户**；未验证邮箱/手机返回 `403`） |
| GET | `/api/v1/cart` | 我的购物车列表（含合计） |
| POST | `/api/v1/cart` | 加入购物车（同 SKU 自动累加） |
| PATCH | `/api/v1/cart/{id}` | 修改数量（传 0 表示移除） |
| DELETE | `/api/v1/cart/{id}` | 移除商品 |
| POST | `/api/v1/cart/checkout` | 结算购物车（生成订单并清空；**与下单共用验证闸门**，未验证返回 `403`） |
| GET | `/api/v1/users` | 用户列表（active_only 过滤） |

> **鉴权**：除 `/health` 与 `/auth/login`、`/auth/register`、`/auth/logout` 外，所有接口都必须携带身份凭证
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
| POST | `/api/v1/categories` | 创建分类 |
| PATCH | `/api/v1/categories/{id}` | 修改分类 |
| DELETE | `/api/v1/categories/{id}` | 删除分类（有商品时拒绝） |
| GET | `/api/v1/orders/{id}` | 订单详情，含明细、可执行动作、流转时间线 |
| POST | `/api/v1/orders/{id}/actions/{event}` | 推进流转（pay/ship/confirm/cancel/refund/approve/reject） |
| GET | `/api/v1/workflows/definitions` | 流程定义列表（`?code=` 按业务 code 过滤版本行） |
| GET | `/api/v1/workflows/definitions/{id}` | 流程定义图（设计器加载用） |
| PUT | `/api/v1/workflows/definitions/{id}` | 更新流程（全量替换节点与流转边） |
| POST | `/api/v1/workflows/definitions/{id}/versions` | 派生新版本（克隆图，version = max+1） |
| GET | `/api/v1/workflows/definitions/code/{code}/versions` | 版本历史（按 version 倒序） |
| POST | `/api/v1/workflows/definitions/{id}/publish` | 发布前校验（必须有 start/end、无悬空引用）；发布时降级同 code 其它 published 版本 |
| GET | `/api/v1/admin/db/tables` | 数据库表浏览（只读） |

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

### ✅ 已实现

- [x] 工作流引擎（数据库驱动、可配置：`start/fire/available_events`、流转日志 + 上下文快照、非法流转显式报错）
- [x] 订单服务（创建订单同事务：校验库存 → 算金额 → 扣库存 → 启动流程 → 推进待付款）
- [x] 库存并发控制（DB 层原子条件 UPDATE：`UPDATE ... WHERE stock >= qty` 靠 `rowcount==0` 判不足；归还用 `stock = stock + qty` 累加，杜绝 TOCTOU 超卖与并发丢失更新）
- [x] 鉴权基础（JWT HS256 + PBKDF2 密码哈希 + 依赖注入身份，前端无法伪造 `user_id`）
- [x] 跨域收紧 + JWT 写入 httpOnly Cookie（CORS 仅放行 `settings.CORS_ORIGINS`；`POST /auth/logout` 清 Cookie）
- [x] RBAC 角色权限（管理员 / 普通买家；用户管理与订单流转仅管理员；买家越权访问统一 404；初始管理员可配置且空值启动报错）
- [x] REST API 全量（auth / products / orders / cart / users+addresses / categories / workflows / admin_db 只读）
- [x] 购物车（同 SKU 累加、累加受库存约束、结算与清空同事务）
- [x] 用户与地址（软删除、路径内 `user_id+address_id` 联合过滤防越权、默认地址互斥）
- [x] 分类（两级树；删除前应用层校验商品/子分类引用，防间接环）
- [x] 前端管理后台（订单管理页、流程设计器、登录页）
- [x] 新建订单可选收货地址（OrdersView 弹窗下拉复用 `/users/{id}/addresses`，按令牌归属拉取；选中才传 `address_id` 补全订单 `address_snapshot`，不选中则订单无快照）+ 后端 `create_order` 地址归属校验防 IDOR（他人 `address_id` 与「不存在」同等处理，统一 404 不泄露是否存在）
- [x] 工具脚本（init_db / seed / export_schema / export_data_html）
- [x] MySQL 8.0.45 实跑验证（建表 / 种子 / 下单 / 流转 / 购物车 / 设计器全链路；方言差异已处理）
- [x] 测试（pytest 全量 150 passed）

### ❌ 待实现

- [x] 购物车前端页面（`CartView.vue`：选 SKU 加购、改数量、移除、合计、选地址结算；数量改动受控渲染，失败回滚到后端真实值）
- [x] 商品管理页面（`ProductsView.vue`：搜索 / 状态筛选含下架、SKU 展开明细、上架下架、新建商品含动态 SKU 行）
- [x] 分类管理页面（`CategoriesView.vue`：树形层级、商品数、新建/编辑/删除、加子分类；有子分类或仍被商品引用时后端拒绝删除）
- [x] 用户管理页面（`UsersView.vue`：用户列表/新建/改资料/软禁用 + 本人收货地址增删改；禁用为软删除且不能禁用当前管理员）
- [x] Alembic 迁移脚本（初始迁移已生成并与模型一致；`alembic upgrade head` / `downgrade base`；`tests/test_migrations.py` 守住「改模型忘写迁移」）
- [x] 流程定义版本管理（同 code 多版本；POST .../versions 派生新草案克隆图、GET .../code/{code}/versions 版本历史；发布保证唯一 published 并自动降级旧版本；回滚=重新发布旧版本）
- [x] JWT 刷新 / 续期机制（短期访问令牌 30 分钟 + 长期刷新令牌 7 天写独立 httpOnly Cookie；POST /auth/refresh 静默换发访问令牌；前端 401 自动刷新并重试一次；access/refresh 令牌 type 隔离防混用）
- [x] 列表接口分页（orders / products / users 统一返回 `{items, total}` 信封；`limit=0` 表示不分页返回全部，保证 SKU 下拉框全量不被截断；total 用子查询统计；前端 OrdersView/ProductsView/UsersView 均加 `el-pagination`）
- [x] 登录限流（POST /auth/login 按 (IP, 用户名) 固定窗口计数失败次数，超阈值返 429 + Retry-After；成功清空计数；单实例内存级，生产换 Redis）
- [x] 订单搜索 / 筛选增强（列表支持关键词：订单号 + 商品行项名称 LIKE；下单时间范围 `created_from`/`created_to` 闭区间；非法日期 400；与 status/分页共用同一过滤条件统计 total）
- [x] 邮箱 / 手机验证（两步式 OTP：send/confirm；可插拔发送器；DB 重发限流；开发回传 dev_code；注册不强制验证，**下单已加 403 强约束闸门**）
