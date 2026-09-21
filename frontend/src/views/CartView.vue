<template>
  <div>
    <!-- 加购区：选 SKU → 数量 → 加入 -->
    <div class="toolbar">
      <el-select
        v-model="skuId"
        placeholder="选择要加入的商品 SKU"
        filterable
        clearable
        style="width: 340px"
      >
        <el-option
          v-for="s in skuOptions"
          :key="s.id"
          :label="`${s.productName} / ${s.spec} (¥${s.price}) 库存${s.stock}`"
          :value="s.id"
        />
      </el-select>
      <!-- 未选 SKU 时不限制上限；选中后以库存为上限（库存 0 时下限取 1，由后端最终裁决） -->
      <el-input-number v-model="addQty" :min="1" :max="skuId ? Math.max(selectedStock, 1) : 999" />
      <el-button type="primary" @click="addItem">加入购物车</el-button>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table :data="items" stripe v-loading="loading">
      <el-table-column prop="product_name" label="商品" />
      <el-table-column prop="spec" label="规格" width="120" />
      <el-table-column label="单价" width="110" align="right">
        <template #default="{ row }">¥{{ row.price.toFixed(2) }}</template>
      </el-table-column>
      <el-table-column label="数量" width="170">
        <template #default="{ row }">
          <!-- 必须用 v-model（乐观更新）而非受控 :model-value：
               el-input-number 内部持有 currentValue，只在 modelValue 这个 prop
               真的发生变化时才重新同步。受控写法下请求失败时 prop 没变，
               组件内部会一直停留在被拒绝的数字上，load() 也拉不回来。
               改为 v-model 后，失败时 load() 会把服务端真实值写回 prop，
               prop 变化即触发内部同步，UI 自动回滚。 -->
          <el-input-number
            v-model="row.quantity"
            :min="1"
            :max="Math.max(row.stock, 1)"
            :value-on-clear="1"
            :disabled="busy[row.id]"
            size="small"
            aria-label="数量"
            @change="(v) => changeQty(row, v)"
          />
        </template>
      </el-table-column>
      <el-table-column label="小计" width="110" align="right">
        <template #default="{ row }">¥{{ row.subtotal.toFixed(2) }}</template>
      </el-table-column>
      <el-table-column label="剩余库存" width="100">
        <template #default="{ row }">
          <!-- 加购后商品被别人买走导致库存下降时，这里要显式告警 -->
          <el-tag v-if="row.quantity > row.stock" type="danger">{{ row.stock }}</el-tag>
          <span v-else>{{ row.stock }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" type="danger" text @click="removeItem(row)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="!loading && !items.length" class="empty">购物车为空，先选一个商品加入吧</div>

    <div class="footer">
      <span class="total">合计：<b>¥{{ total.toFixed(2) }}</b></span>
      <el-select
        v-model="addressId"
        placeholder="收货地址（可选）"
        clearable
        style="width: 300px"
      >
        <el-option
          v-for="a in addresses"
          :key="a.id"
          :label="`${a.receiver} ${a.phone} ${a.province}${a.city}${a.district}${a.detail}`"
          :value="a.id"
        />
      </el-select>
      <el-button type="primary" :disabled="!items.length" @click="doCheckout">结算</el-button>
    </div>

    <p class="tip">
      结算与「清空购物车」由后端在同一事务内完成：下单失败时购物车保留，不会出现「订单没生成、购物车却被清空」。
    </p>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const items = ref([])
const loading = ref(false)
const skuOptions = ref([])
const skuId = ref(null)
const addQty = ref(1)
const addresses = ref([])
const addressId = ref(null)
// 行级「请求进行中」标记：连点 +/- 会并发发出多个 PATCH，响应乱序回来会把
// 过期值写回 UI。进行中禁用该行输入，保证一次只飞一个请求。
const busy = reactive({})

// 合计本地计算：改数量后立刻同步，避免依赖后端返回的 total（改数量后已过期）
const total = computed(() =>
  items.value.reduce((sum, i) => sum + i.price * i.quantity, 0)
)
const selectedStock = computed(
  () => skuOptions.value.find((s) => s.id === skuId.value)?.stock || 0
)

async function load() {
  loading.value = true
  try {
    const cart = await api.getCart()
    items.value = cart.items || []
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadSkuOptions(force = false) {
  // force=true 用于加购后刷新库存上限；默认有缓存就直接用
  if (skuOptions.value.length && !force) return
  try {
    const products = await api.listProducts()
    skuOptions.value = products.flatMap((p) =>
      // 只列在售 SKU：后端对下架 SKU 会直接 400「已下架」，
      // 与其让用户选了再报错，不如一开始就不列出来
      p.skus
        .filter((s) => s.status === 'on_sale')
        .map((s) => ({
          id: s.id,
          spec: s.spec,
          price: s.price,
          stock: s.stock,
          productName: p.name,
        }))
    )
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function loadAddresses() {
  try {
    // 地址必须经 /users/{id}/addresses 取（归属校验），用户详情接口不返回地址
    const me = await api.me()
    addresses.value = await api.listAddresses(me.id)
    const def = addresses.value.find((a) => a.is_default)
    if (def) addressId.value = def.id
  } catch {
    // 地址拉不到不影响加购与结算（address_id 可选），静默降级
    addresses.value = []
  }
}

async function addItem() {
  if (!skuId.value) return ElMessage.warning('请先选择商品 SKU')
  try {
    await api.addToCart({ sku_id: skuId.value, quantity: addQty.value })
    ElMessage.success('已加入购物车')
    addQty.value = 1
    await load()
    await loadSkuOptions(true) // 库存可能变化，强制刷新可选数量上限
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function changeQty(row, value) {
  // 清空输入框时可能为 null（value-on-clear 已兜底为 1，这里再防御一次）
  if (value == null || Number.isNaN(value)) {
    await load()
    return
  }
  // 注意：不能加 `if (value === row.quantity) return` —— v-model 在 @change 触发前
  // 已经把 row.quantity 改成新值了，加上这句会导致永远不发请求。
  if (busy[row.id]) return // 进行中：忽略连点，避免乱序回写
  busy[row.id] = true
  try {
    // 传 0 等价于删除该行；这里最小值为 1，故只可能是改数量
    await api.updateCartItem(row.id, { quantity: value })
    row.subtotal = +(row.price * value).toFixed(2)
  } catch (e) {
    ElMessage.error(e.message)
    // v-model 已乐观更新本地值；load() 把服务端真实值写回 prop，
    // prop 变化即触发组件内部同步，UI 自动回滚到真实值
    await load()
  } finally {
    busy[row.id] = false
  }
}

async function removeItem(row) {
  try {
    await api.removeCartItem(row.id)
    ElMessage.success('已移除')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
    // 例如该行已在别处被删（404）：必须刷新，否则残留一行幽灵数据
    await load()
  }
}

async function doCheckout() {
  if (!items.value.length) return
  const oversell = items.value.filter((i) => i.quantity > i.stock)
  if (oversell.length) {
    return ElMessage.error(
      `「${oversell.map((i) => i.product_name).join('、')}」数量超过库存，请先调整`
    )
  }
  try {
    await ElMessageBox.confirm(
      `本次结算 ${items.value.length} 个商品，合计 ¥${total.value.toFixed(2)}。结算后购物车将清空。`,
      '确认结算',
      { type: 'warning' }
    )
  } catch {
    return // 用户取消
  }
  try {
    const r = await api.checkout({ address_id: addressId.value ?? undefined })
    ElMessage.success(`结算成功，订单 ${r.order_no} 已生成`)
    await load()
  } catch (e) {
    ElMessage.error(e.message)
    // 结算失败购物车必须还在：刷新一次让用户看到真实状态
    await load()
  }
}

onMounted(async () => {
  // 三者互不依赖，并发拉取减少串行等待
  await Promise.all([load(), loadSkuOptions(), loadAddresses()])
})
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.footer {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 16px;
  flex-wrap: wrap;
}
.total {
  font-size: 15px;
}
.total b {
  color: #f56c6c;
  font-size: 18px;
}
.empty {
  color: #8b949e;
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
.tip {
  color: #8b949e;
  font-size: 12px;
  margin: 10px 0 0;
}
</style>
