<template>
  <div>
    <div class="toolbar">
      <el-input
        v-model="keyword"
        placeholder="按商品名搜索"
        clearable
        style="width: 220px"
        @keyup.enter="load"
        @clear="load"
      />
      <!-- 传空 status = 不筛选，这样下架商品也能在管理页看到并重新上架 -->
      <el-select v-model="status" style="width: 150px" @change="load">
        <el-option label="全部（含下架）" value="" />
        <el-option label="仅在售" value="on_sale" />
        <el-option label="仅下架" value="off_shelf" />
      </el-select>
      <el-button @click="load">刷新</el-button>
      <el-button type="primary" @click="openCreate">新建商品</el-button>
    </div>

    <el-table :data="list" stripe v-loading="loading">
      <el-table-column type="expand">
        <template #default="{ row }">
          <el-table :data="row.skus" size="small" class="sku-table">
            <el-table-column prop="sku_code" label="SKU 编码" width="160" />
            <el-table-column prop="spec" label="规格" width="140" />
            <el-table-column label="价格" width="110" align="right">
              <template #default="{ row: s }">¥{{ s.price.toFixed(2) }}</template>
            </el-table-column>
            <el-table-column prop="stock" label="库存" width="90" />
            <el-table-column prop="status" label="状态" width="110">
              <template #default="{ row: s }">
                <el-tag :type="s.status === 'on_sale' ? 'success' : 'info'" size="small">
                  {{ s.status === 'on_sale' ? '在售' : '下架' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="!row.skus.length" class="tip">该商品暂无 SKU</div>
        </template>
      </el-table-column>

      <el-table-column prop="name" label="商品名称" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.status === 'on_sale' ? 'success' : 'info'">
            {{ row.status === 'on_sale' ? '在售' : '已下架' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="SKU 数" width="90">
        <template #default="{ row }">{{ row.skus.length }}</template>
      </el-table-column>
      <el-table-column label="库存合计" width="110">
        <template #default="{ row }">
          {{ row.skus.reduce((s, i) => s + i.stock, 0) }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-button
            size="small"
            :type="row.status === 'on_sale' ? 'warning' : 'success'"
            @click="toggleShelf(row)"
          >
            {{ row.status === 'on_sale' ? '下架' : '上架' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div v-if="!loading && !list.length" class="empty">暂无商品</div>

    <!-- 新建商品：名称 + 分类 + 若干 SKU -->
    <el-dialog v-model="createVisible" title="新建商品" width="720px">
      <el-form label-width="90px">
        <el-form-item label="商品名称" required>
          <el-input v-model="form.name" placeholder="必填" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="封面 URL">
          <el-input v-model="form.cover" placeholder="可选" />
        </el-form-item>
        <el-form-item label="分类">
          <!-- allow-create：后端对不存在的分类名会自动创建，这里允许直接输入新分类 -->
          <el-select
            v-model="form.category_name"
            filterable
            allow-create
            clearable
            default-first-option
            placeholder="选择或直接输入新分类"
            style="width: 100%"
          >
            <el-option v-for="c in categories" :key="c.id" :label="c.name" :value="c.name" />
          </el-select>
        </el-form-item>
      </el-form>

      <div class="sku-header">
        <h4>SKU（至少一个）</h4>
        <el-button size="small" @click="addSkuRow">添加一行</el-button>
      </div>
      <el-table :data="form.skus" size="small" border>
        <el-table-column label="SKU 编码" width="160">
          <template #default="{ row }">
            <el-input v-model="row.sku_code" size="small" placeholder="唯一" />
          </template>
        </el-table-column>
        <el-table-column label="规格">
          <template #default="{ row }">
            <el-input v-model="row.spec" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="价格" width="150">
          <template #default="{ row }">
            <el-input-number
              v-model="row.price"
              :min="0.01"
              :precision="2"
              :step="1"
              size="small"
              controls-position="right"
            />
          </template>
        </el-table-column>
        <el-table-column label="库存" width="140">
          <template #default="{ row }">
            <el-input-number
              v-model="row.stock"
              :min="0"
              :precision="0"
              size="small"
              controls-position="right"
            />
          </template>
        </el-table-column>
        <el-table-column label="" width="70">
          <template #default="{ $index }">
            <el-button
              size="small"
              type="danger"
              text
              :disabled="form.skus.length === 1"
              @click="removeSkuRow($index)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" @click="submitCreate">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const list = ref([])
const loading = ref(false)
const keyword = ref('')
// 空字符串 = 不按状态筛选（后端 `if status:` 才加 where），便于管理下架商品
const status = ref('')
const categories = ref([])
const createVisible = ref(false)
const form = ref(emptyForm())

function emptyForm() {
  return {
    name: '',
    description: '',
    cover: '',
    category_name: '',
    skus: [{ sku_code: '', spec: '', price: 1, stock: 0 }],
  }
}

async function load() {
  loading.value = true
  try {
    list.value = await api.listProducts({ keyword: keyword.value, status: status.value })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadCategories() {
  try {
    categories.value = await api.listCategories()
  } catch {
    categories.value = [] // 分类拉不到不影响新建（可手输新分类）
  }
}

function addSkuRow() {
  form.value.skus.push({ sku_code: '', spec: '', price: 1, stock: 0 })
}

function removeSkuRow(i) {
  form.value.skus.splice(i, 1)
}

async function openCreate() {
  form.value = emptyForm()
  if (!categories.value.length) await loadCategories()
  createVisible.value = true
}

async function submitCreate() {
  const f = form.value
  if (!f.name.trim()) return ElMessage.warning('请填写商品名称')

  const skus = f.skus.filter((s) => s.sku_code.trim())
  if (!skus.length) return ElMessage.warning('至少填写一个 SKU 编码')
  if (skus.some((s) => !(s.price > 0))) return ElMessage.warning('SKU 价格必须大于 0')

  try {
    await api.createProduct({
      name: f.name.trim(),
      description: f.description,
      cover: f.cover,
      category_name: f.category_name || '',
      skus: skus.map((s) => ({
        sku_code: s.sku_code.trim(),
        spec: s.spec,
        price: s.price,
        stock: s.stock,
      })),
    })
    ElMessage.success('商品已创建')
    createVisible.value = false
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function toggleShelf(row) {
  const onSale = row.status !== 'on_sale'
  try {
    await api.setShelf(row.id, onSale)
    ElMessage.success(onSale ? '已上架' : '已下架')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(async () => {
  await Promise.all([load(), loadCategories()])
})
</script>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.sku-table {
  margin: 8px 0;
}
.sku-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 6px;
}
h4 {
  margin: 0;
  font-size: 14px;
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
  margin: 6px 0 0;
}
</style>
