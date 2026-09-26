<template>
  <div>
    <div class="toolbar">
      <span class="hint">低库存阈值</span>
      <el-input-number
        v-model="lowStockThreshold"
        :min="0"
        :step="5"
        size="small"
        controls-position="right"
        style="width: 120px"
        @change="load"
      />
      <el-select v-model="recentDays" size="small" style="width: 120px" @change="load">
        <el-option label="近 7 天" :value="7" />
        <el-option label="近 30 天" :value="30" />
      </el-select>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <div v-loading="loading">
      <div class="cards">
        <el-card v-for="c in cards" :key="c.label" shadow="hover" class="stat-card">
          <div class="label">{{ c.label }}</div>
          <div class="value">{{ c.value }}</div>
          <div class="sub">{{ c.sub }}</div>
        </el-card>
      </div>

      <div class="section-title">订单状态分布</div>
      <div class="statuses">
        <el-tag v-for="(count, status) in byStatus" :key="status" :type="statusTagType(status)">
          {{ statusLabel(status) }}：{{ count }}
        </el-tag>
        <span v-if="!Object.keys(byStatus).length" class="tip">暂无订单</span>
      </div>

      <div class="section-title">近 {{ recentDays }} 天趋势</div>
      <el-table :data="trend" size="small" stripe>
        <el-table-column prop="date" label="日期" width="140" />
        <el-table-column prop="orders" label="订单数" width="120" align="right" />
        <el-table-column label="销售额" align="right">
          <template #default="{ row }">{{ money(row.paid_amount) }}</template>
        </el-table-column>
        <el-table-column label="" />
      </el-table>
      <div v-if="!trend.length && !loading" class="tip">该区间暂无订单</div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

// 与后端 Order.status 取值一致
const STATUS_LABELS = {
  pending_payment: '待付款',
  paid: '待发货',
  shipped: '已发货',
  completed: '已完成',
  closed: '已关闭',
}

const loading = ref(false)
const overview = ref(null)
const trend = ref([])
const lowStockThreshold = ref(10)
const recentDays = ref(7)

function money(v) {
  const n = Number(v || 0)
  return `¥${n.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status
}

function statusTagType(status) {
  if (status === 'completed') return 'success'
  if (status === 'closed') return 'info'
  if (status === 'shipped') return 'warning'
  return 'primary'
}

const byStatus = computed(() => overview.value?.orders?.by_status || {})

const cards = computed(() => {
  const o = overview.value
  if (!o) return []
  return [
    { label: '订单总数', value: o.orders.total, sub: `近 ${o.recent.days} 天 ${o.recent.orders} 单` },
    { label: '销售额（GMV）', value: money(o.orders.paid_amount), sub: '仅统计已支付订单' },
    { label: '在售商品', value: o.products.on_sale, sub: `共 ${o.products.total} 个（下架 ${o.products.off_shelf}）` },
    { label: '库存合计', value: o.products.stock_total, sub: `${o.products.sku_total} 个 SKU` },
    {
      label: '低库存 SKU',
      value: o.products.low_stock,
      sub: `库存 ≤ ${o.products.low_stock_threshold}`,
    },
    { label: '活跃用户', value: o.users.active_total, sub: '未禁用账号' },
  ]
})

async function load() {
  loading.value = true
  try {
    // 两个请求互不依赖，并发拉取
    const [o, t] = await Promise.all([
      api.stats({ low_stock_threshold: lowStockThreshold.value, recent_days: recentDays.value }),
      api.statsTrend(recentDays.value),
    ])
    overview.value = o
    trend.value = t.items || []
  } catch (e) {
    // 统计接口是 require_admin：非管理员进来只会拿到 403，提示清楚即可
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.hint {
  color: #8b949e;
  font-size: 13px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.stat-card .label {
  color: #8b949e;
  font-size: 13px;
}
.stat-card .value {
  font-size: 24px;
  font-weight: 600;
  margin: 6px 0 4px;
}
.stat-card .sub {
  color: #8b949e;
  font-size: 12px;
}
.section-title {
  margin: 20px 0 10px;
  font-size: 14px;
  font-weight: 600;
}
.statuses {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.tip {
  color: #8b949e;
  font-size: 13px;
  padding: 8px 0;
}
</style>
