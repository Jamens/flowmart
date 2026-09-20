# flowmart 数据库表结构

> 由 `backend/scripts/export_schema.py` 自动生成，请勿手工编辑。

## 电商域

### `users`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `username` | VARCHAR(64) | 否 |  | |
| `nickname` | VARCHAR(64) | 否 |  | |
| `phone` | VARCHAR(20) | 否 |  | |
| `is_active` | BOOLEAN | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `addresses`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `user_id` | INTEGER | 否 |  | |
| `receiver` | VARCHAR(64) | 否 |  | |
| `phone` | VARCHAR(20) | 否 |  | |
| `province` | VARCHAR(64) | 否 |  | |
| `city` | VARCHAR(64) | 否 |  | |
| `district` | VARCHAR(64) | 否 |  | |
| `detail` | VARCHAR(255) | 否 |  | |
| `is_default` | BOOLEAN | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `categories`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `name` | VARCHAR(64) | 否 |  | |
| `parent_id` | INTEGER | 否 |  | |
| `sort` | INTEGER | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `products`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `category_id` | INTEGER | 否 |  | |
| `name` | VARCHAR(128) | 否 |  | |
| `description` | TEXT | 否 |  | |
| `cover` | VARCHAR(255) | 否 |  | |
| `status` | VARCHAR(20) | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `skus`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `product_id` | INTEGER | 否 |  | |
| `sku_code` | VARCHAR(64) | 否 |  | |
| `spec` | VARCHAR(255) | 否 |  | |
| `price` | NUMERIC(12, 2) | 否 |  | |
| `stock` | INTEGER | 否 |  | |
| `status` | VARCHAR(20) | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `cart_items`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `user_id` | INTEGER | 否 |  | |
| `sku_id` | INTEGER | 否 |  | |
| `quantity` | INTEGER | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `orders`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `order_no` | VARCHAR(32) | 否 |  | |
| `user_id` | INTEGER | 否 |  | |
| `total_amount` | NUMERIC(12, 2) | 否 |  | |
| `pay_amount` | NUMERIC(12, 2) | 否 |  | |
| `status` | VARCHAR(32) | 否 |  | |
| `current_node_key` | VARCHAR(64) | 否 |  | |
| `workflow_instance_id` | INTEGER | 是 |  | |
| `address_snapshot` | TEXT | 否 |  | |
| `remark` | VARCHAR(255) | 否 |  | |
| `paid_at` | DATETIME | 是 |  | |
| `shipped_at` | DATETIME | 是 |  | |
| `finished_at` | DATETIME | 是 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `order_items`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `order_id` | INTEGER | 否 |  | |
| `sku_id` | INTEGER | 否 |  | |
| `sku_name` | VARCHAR(128) | 否 |  | |
| `spec` | VARCHAR(255) | 否 |  | |
| `price` | NUMERIC(12, 2) | 否 |  | |
| `quantity` | INTEGER | 否 |  | |
| `subtotal` | NUMERIC(12, 2) | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `payments`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `order_id` | INTEGER | 否 |  | |
| `pay_no` | VARCHAR(32) | 否 |  | |
| `amount` | NUMERIC(12, 2) | 否 |  | |
| `channel` | VARCHAR(20) | 否 |  | |
| `status` | VARCHAR(20) | 否 |  | |
| `paid_at` | DATETIME | 是 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

## 工作流域

### `wf_definitions`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `code` | VARCHAR(64) | 否 |  | |
| `name` | VARCHAR(128) | 否 |  | |
| `description` | TEXT | 否 |  | |
| `version` | INTEGER | 否 |  | |
| `status` | VARCHAR(20) | 否 |  | |
| `created_at` | DATETIME | 否 |  | |
| `updated_at` | DATETIME | 否 |  | |

### `wf_nodes`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `definition_id` | INTEGER | 否 |  | |
| `key` | VARCHAR(64) | 否 |  | |
| `name` | VARCHAR(128) | 否 |  | |
| `node_type` | VARCHAR(20) | 否 |  | |
| `x` | INTEGER | 否 |  | |
| `y` | INTEGER | 否 |  | |
| `meta` | JSON | 否 |  | |

### `wf_transitions`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `definition_id` | INTEGER | 否 |  | |
| `from_node_key` | VARCHAR(64) | 否 |  | |
| `to_node_key` | VARCHAR(64) | 否 |  | |
| `event` | VARCHAR(64) | 否 |  | |
| `condition_expr` | TEXT | 否 |  | |
| `priority` | INTEGER | 否 |  | |
| `description` | VARCHAR(255) | 否 |  | |

### `wf_instances`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `definition_id` | INTEGER | 否 |  | |
| `biz_type` | VARCHAR(64) | 否 |  | |
| `biz_id` | VARCHAR(64) | 否 |  | |
| `current_node_key` | VARCHAR(64) | 否 |  | |
| `status` | VARCHAR(20) | 否 |  | |
| `context` | JSON | 否 |  | |
| `started_at` | DATETIME | 否 |  | |
| `finished_at` | DATETIME | 是 |  | |

### `wf_transition_logs`

| 字段 | 类型 | 允许空 | 主键 | 说明 |
| --- | --- | --- | --- | --- |
| `id` | INTEGER | 否 | ✅ | |
| `instance_id` | INTEGER | 否 |  | |
| `from_node_key` | VARCHAR(64) | 否 |  | |
| `to_node_key` | VARCHAR(64) | 否 |  | |
| `event` | VARCHAR(64) | 否 |  | |
| `operator` | VARCHAR(64) | 否 |  | |
| `comment` | VARCHAR(255) | 否 |  | |
| `snapshot` | JSON | 否 |  | |
| `created_at` | DATETIME | 否 |  | |

