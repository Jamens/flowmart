<template>
  <div>
    <!-- 工具栏 -->
    <div class="toolbar">
      <el-select v-model="status" placeholder="按状态筛选" clearable @change="onFilterChange" style="width: 180px">
        <el-option v-for="(label, key) in STATUS" :key="key" :label="label" :value="key" />
      </el-select>
      <!-- 关键词：订单号或商品名（后端匹配 order_no + 订单行项 sku_name 快照） -->
      <el-input
        v-model="keyword"
        placeholder="订单号 / 商品名"
        clearable
        style="width: 200px"
        @keyup.enter="onFilterChange"
        @clear="onFilterChange"
      />
      <!-- 下单时间范围，闭区间含当天；空表示不限制 -->
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        range-separator="至"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
        style="width: 260px"
        @change="onFilterChange"
      />
      <el-button @click="load">刷新</el-button>
      <el-button type="primary" @click="openCreate">新建订单</el-button>
    </div>

    <el-table :data="list" stripe v-loading="loading">
      <el-table-column prop="order_no" label="订单号" width="210" />
      <el-table-column label="金额" width="110" align="right">
        <template #default="{ row }">¥{{ row.pay_amount.toFixed(2) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <el-tag :type="tagType(row.status)">{{ STATUS[row.status] || row.status }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="商品">
        <template #default="{ row }">
          {{ row.items.map((i) => `${i.sku_name}×${i.quantity}`).join('，') }}
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="下单时间" width="180" />
      <el-table-column label="操作" width="100">
        <template #default="{ row }">
          <el-button size="small" @click="showDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      class="pager"
      layout="total, sizes, prev, pager, next"
      :total="total"
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :page-sizes="[10, 20, 50]"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <!-- 订单详情：明细 + 可执行流转 + 流转时间线 -->
    <el-drawer v-model="drawer" :title="`订单 ${detail?.order_no || ''}`" size="46%">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="金额">¥{{ detail.pay_amount.toFixed(2) }}</el-descriptions-item>
          <el-descriptions-item label="当前状态">
            <el-tag :type="tagType(detail.status)">{{ STATUS[detail.status] || detail.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="收货信息" :span="2">{{ detail.address_snapshot || '-' }}</el-descriptions-item>
        </el-descriptions>

        <h4>商品明细</h4>
        <el-table :data="detail.items" size="small">
          <el-table-column prop="sku_name" label="商品" />
          <el-table-column prop="spec" label="规格" width="120" />
          <el-table-column prop="price" label="单价" width="100" />
          <el-table-column prop="quantity" label="数量" width="80" />
          <el-table-column prop="subtotal" label="小计" width="100" />
        </el-table>

        <!-- 可执行动作由引擎返回，前端不做任何状态判断 -->
        <h4>可执行操作</h4>
        <div class="actions">
          <el-button
            v-for="e in detail.available_events"
            :key="e.event"
            :type="e.event === 'cancel' || e.event === 'reject' ? 'danger' : 'primary'"
            size="small"
            @click="fire(e)"
          >
            {{ EVENT[e.event] || e.event }}
          </el-button>
          <span v-if="!detail.available_events.length" class="tip">该订单已到终态，无可执行操作</span>
        </div>
        <p v-if="detail.available_events.length" class="tip">
          按钮由工作流引擎根据当前节点与条件动态返回，新增流程节点无需改动前端代码。
        </p>

        <h4>流转时间线</h4>
        <el-timeline>
          <el-timeline-item
            v-for="(t, i) in detail.timeline"
            :key="i"
            :timestamp="t.created_at"
            placement="top"
          >
            {{ t.from ? `${STATUS[t.from] || t.from} → ${STATUS[t.to] || t.to}` : '流程启动' }}
            <div class="tip">
              事件 <code>{{ t.event }}</code> · 操作人 {{ t.operator }}
              <span v-if="t.comment"> · {{ t.comment }}</span>
            </div>
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-drawer>

    <!-- 新建订单 -->
    <el-dialog v-model="createVisible" title="新建订单" width="460px">
      <el-form label-width="90px">
        <el-form-item label="商品 SKU">
          <el-select v-model="form.sku_id" placeholder="选择 SKU" style="width: 100%">
            <el-option
              v-for="s in skuOptions"
              :key="s.id"
              :label="`${s.productName} / ${s.spec} (¥${s.price}) 库存${s.stock}`"
              :value="s.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="数量">
          <el-input-number v-model="form.quantity" :min="1" />
        </el-form-item>
        <el-form-item label="收货地址">
          <el-select v-model="form.address_id" placeholder="选择收货地址（可不选）" clearable style="width: 100%">
            <el-option
              v-for="a in addresses"
              :key="a.id"
              :label="`${a.receiver} ${a.phone}｜${a.province}${a.city}${a.district}${a.detail}${a.is_default ? '（默认）' : ''}`"
              :value="a.id"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreate">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

// 节点 key -> 中文。注意这里只做展示映射，不包含任何流转规则
const STATUS = {
  start: '开始',
  pending_payment: '待付款',
  paid: '待发货',
  shipped: '已发货',
  completed: '已完成',
  closed: '已关闭',
  refunding: '退款审核中',
}
const EVENT = {
  submit: '提交订单',
  pay: '支付',
  ship: '发货',
  confirm: '确认收货',
  cancel: '取消订单',
  refund: '申请退款',
  approve: '审核通过',
  reject: '驳回退款',
}

const list = ref([])
const status = ref('')
const keyword = ref('')
const dateRange = ref([])
const loading = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const drawer = ref(false)
const detail = ref(null)
const createVisible = ref(false)
const skuOptions = ref([])
const addresses = ref([])
const currentUserId = ref(null)
const form = ref({ sku_id: null, quantity: 1, address_id: null })

function tagType(s) {
  if (s === 'completed') return 'success'
  if (s === 'closed') return 'info'
  if (s === 'pending_payment' || s === 'refunding') return 'warning'
  return 'primary'
}

async function load() {
  loading.value = true
  try {
    // 列表接口返回 {items, total}：total 是过滤后的总数，与当前页无关
    const res = await api.listOrders({
      status: status.value,
      keyword: keyword.value,
      created_from: dateRange.value?.[0] || '',
      created_to: dateRange.value?.[1] || '',
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    list.value = res.items
    total.value = res.total
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  page.value = 1 // 换筛选条件必须回到第一页，否则会停在一个越界的空页上
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

async function showDetail(row) {
  try {
    detail.value = await api.getOrder(row.id)
    drawer.value = true
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function fire(e) {
  try {
    const updated = await api.fireEvent(detail.value.id, e.event, { operator: 'admin' })
    ElMessage.success(`已执行「${EVENT[e.event] || e.event}」`)
    detail.value = updated
    await load()
  } catch (err) {
    ElMessage.error(err.message)
  }
}

async function openCreate() {
  form.value = { sku_id: null, quantity: 1, address_id: null }
  try {
    // 收货地址强制走归属校验（路径 user_id 必须等于令牌用户），故先取当前用户再拉地址列表
    if (!currentUserId.value) currentUserId.value = (await api.me()).id
    addresses.value = await api.listAddresses(currentUserId.value)
  } catch (e) {
    ElMessage.error(e.message)
  }
  if (!skuOptions.value.length) {
    // 下拉框要全量商品（不分页），用 listAllProducts 而不是分页的 listProducts
    const products = await api.listAllProducts()
    skuOptions.value = products.flatMap((p) =>
      p.skus.map((s) => ({ id: s.id, spec: s.spec, price: s.price, stock: s.stock, productName: p.name }))
    )
  }
  createVisible.value = true
}

async function submitCreate() {
  if (!form.value.sku_id) return ElMessage.warning('请选择 SKU')
  try {
    // 后端按当前登录用户归属订单（忽略请求体 user_id，防冒充）；address_id 选中才传，
    // 后端据此生成 address_snapshot；不传则订单无收货快照（详情页显示「-」）
    const payload = {
      items: [{ sku_id: form.value.sku_id, quantity: form.value.quantity }],
    }
    if (form.value.address_id) payload.address_id = form.value.address_id
    await api.createOrder(payload)
    ElMessage.success('下单成功，流程已自动启动')
    createVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(load)
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}
.pager {
  margin-top: 14px;
  justify-content: flex-end;
}
h4 {
  margin: 18px 0 8px;
  font-size: 14px;
}
.actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.tip {
  color: #8b949e;
  font-size: 12px;
  margin: 6px 0 0;
}
code {
  background: #1c2230;
  padding: 1px 5px;
  border-radius: 3px;
}
</style>
