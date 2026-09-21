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

前端已通过 Vite proxy 把 `/api` 转发到后端，无需额外配置跨域。

## 数据库切换

默认 SQLite（文件 `flowmart.db`）。切到 MySQL 只需设置环境变量或 `.env`：

```bash
DB_DIALECT=mysql
DB_PASSWORD=1234560
```

## 已完成功能

### 工作流引擎（`app/services/workflow_engine.py`）

- `start()` 启动流程实例，初始停在 start 节点
- `fire()` 按「当前节点 + 事件 + 条件」匹配流转边，多条候选按 priority 取第一条满足条件的
- `available_events()` 返回当前可执行事件，供前端渲染按钮
- 每次流转自动写 `wf_transition_logs`，并保存当时的上下文快照用于事后复盘
- 到达 end 节点自动将实例置为 finished
- 非法流转显式报错，不静默忽略

### 订单服务（`app/services/order_service.py`）

- 创建订单：校验库存 → 算金额 → 扣库存 → 启动流程 → 推进到待付款，**同一事务**
- 副作用与流转解耦：`SIDE_EFFECTS` 按事件名注册（pay 记流水、cancel/refund 归还库存）
- 未注册副作用的新事件（如 approve、reject）默认纯推进，无需改动任何代码
- 订单表冗余 `status` 字段供列表筛选，由引擎同步写入

### REST API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/products` | 商品列表（含 SKU） |
| POST | `/api/v1/products` | 创建商品 |
| GET | `/api/v1/orders` | 订单列表，支持按 status 筛选 |
| POST | `/api/v1/orders` | 创建订单（自动启动工作流） |
| GET | `/api/v1/cart?user_id=` | 购物车列表（含合计） |
| POST | `/api/v1/cart` | 加入购物车（同 SKU 自动累加） |
| PATCH | `/api/v1/cart/{id}` | 修改数量（传 0 表示移除） |
| DELETE | `/api/v1/cart/{id}` | 移除商品 |
| POST | `/api/v1/cart/checkout` | 结算购物车（生成订单并清空） |
| GET | `/api/v1/orders/{id}` | 订单详情，含明细、可执行动作、流转时间线 |
| POST | `/api/v1/orders/{id}/actions/{event}` | 推进流转（pay/ship/confirm/cancel/refund/approve/reject） |
| GET | `/api/v1/workflows/definitions` | 流程定义列表 |
| GET | `/api/v1/workflows/definitions/{id}` | 流程定义图（设计器加载用） |
| PUT | `/api/v1/workflows/definitions/{id}` | 更新流程（全量替换节点与流转边） |
| POST | `/api/v1/workflows/definitions/{id}/publish` | 发布前校验（必须有 start/end、无悬空引用） |
| GET | `/api/v1/admin/db/tables` | 数据库表浏览（只读） |

### 购物车（`backend/app/api/cart.py`）

- 同一 SKU 重复加入**累加数量**而非新增一行，避免同一商品在列表里出现多次
- 累加后仍受库存约束，不能靠反复加入突破上限
- 结算与「清空购物车」在同一事务内完成：下单失败时购物车保留，
  不会出现「订单没生成、购物车却被清空」

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

## 待办

- [ ] 用户认证与鉴权（当前订单归属固定在 `user_id=1`，仅为演示）
- [ ] 购物车前端页面（后端 API 已完整并测试通过）
- [ ] `users` / `addresses` / `categories` 的 CRUD 接口（有表无接口）
- [ ] Alembic 迁移脚本（当前用 `create_all`，模型变更后需删表重建）
- [ ] 库存并发控制（高并发下需要行锁或乐观锁）
- [ ] 流程定义版本管理（当前同 code 只允许一个 published 版本）
