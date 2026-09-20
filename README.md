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

# 4. 启动服务
cd backend && python -m uvicorn app.main:app --reload
```

打开 http://127.0.0.1:8000/docs 查看 Swagger 接口文档。

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
| GET | `/api/v1/orders/{id}` | 订单详情，含明细、可执行动作、流转时间线 |
| POST | `/api/v1/orders/{id}/actions/{event}` | 推进流转（pay/ship/confirm/cancel/refund/approve/reject） |
| GET | `/api/v1/workflows/definitions` | 流程定义列表 |
| GET | `/api/v1/workflows/definitions/{id}` | 流程定义图（设计器加载用） |
| PUT | `/api/v1/workflows/definitions/{id}` | 更新流程（全量替换节点与流转边） |
| POST | `/api/v1/workflows/definitions/{id}/publish` | 发布前校验（必须有 start/end、无悬空引用） |
| GET | `/api/v1/admin/db/tables` | 数据库表浏览（只读） |

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

## 待办

- [ ] 前端管理后台（Vue3 + Element Plus）
- [ ] 可视化流程设计器（拖拽编排）
- [ ] 用户认证与鉴权
- [ ] Alembic 迁移脚本（当前用 create_all）
- [ ] 库存并发控制（高并发下需行锁或乐观锁）
