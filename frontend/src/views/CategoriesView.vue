<template>
  <div>
    <div class="toolbar">
      <el-button @click="load">刷新</el-button>
      <el-button type="primary" @click="openCreate(null)">新建分类</el-button>
      <span class="tip">父分类为空表示顶级分类</span>
    </div>

    <!-- 用 /categories/tree 的嵌套结构展示层级；row-key + tree-props 是 el-table 树形必需 -->
    <el-table
      :data="tree"
      row-key="id"
      :tree-props="{ children: 'children' }"
      default-expand-all
      empty-text="暂无分类"
      v-loading="loading"
      stripe
    >
      <el-table-column prop="name" label="分类名称" />
      <el-table-column label="商品数" width="100" align="right">
        <template #default="{ row }">{{ row.product_count }}</template>
      </el-table-column>
      <el-table-column prop="sort" label="排序" width="90" />
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <el-button size="small" @click="openCreate(row)">加子分类</el-button>
          <el-button size="small" type="danger" text @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="visible" :title="editing ? '编辑分类' : '新建分类'" width="460px">
      <el-form label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="form.name" maxlength="64" show-word-limit placeholder="必填" />
        </el-form-item>
        <el-form-item label="父分类">
          <!-- 编辑时排除自己：把父级设成自己后端会直接判成环 -->
          <el-select v-model="form.parent_id" style="width: 100%">
            <el-option label="（顶级分类）" :value="0" />
            <el-option
              v-for="c in parentOptions"
              :key="c.id"
              :label="c.name"
              :value="c.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="排序">
          <el-input-number v-model="form.sort" :min="0" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="visible = false">取消</el-button>
        <el-button type="primary" @click="submit">提交</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const tree = ref([])
const flat = ref([])
const loading = ref(false)
const visible = ref(false)
const editing = ref(null)
const form = ref({ name: '', parent_id: 0, sort: 0 })

function descendantIds(node) {
  const ids = []
  const walk = (n) => {
    for (const ch of n.children || []) {
      ids.push(ch.id)
      walk(ch)
    }
  }
  walk(node)
  return ids
}

// 编辑时不能选自己，也不能选自己的任意子孙 —— 否则层级成环，
// 两个节点会互相成为对方的 children，谁都不会出现在树里（后端虽有 400 兜底，
// 但能从选项里去掉就别让用户撞上）
const parentOptions = computed(() => {
  if (!editing.value) return flat.value
  const banned = new Set([editing.value.id, ...descendantIds(editing.value)])
  return flat.value.filter((c) => !banned.has(c.id))
})

async function load() {
  loading.value = true
  try {
    // 树用于展示层级，扁平列表用于父分类下拉
    tree.value = await api.listCategoryTree()
    flat.value = await api.listCategories()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function openCreate(parent = null) {
  editing.value = null
  form.value = { name: '', parent_id: parent ? parent.id : 0, sort: 0 }
  visible.value = true
}

function openEdit(row) {
  editing.value = row
  form.value = {
    name: row.name,
    parent_id: row.parent_id || 0,
    sort: row.sort || 0,
  }
  visible.value = true
}

async function submit() {
  if (!form.value.name.trim()) return ElMessage.warning('请填写分类名称')
  const payload = {
    name: form.value.name.trim(),
    // 清空数字输入框会 emit null，而 sort 列 NOT NULL，传 null 会让后端 500
    sort: form.value.sort ?? 0,
    parent_id: form.value.parent_id || 0,
  }
  try {
    if (editing.value) {
      await api.updateCategory(editing.value.id, payload)
      ElMessage.success('已更新')
    } else {
      await api.createCategory(payload)
      ElMessage.success('已创建')
    }
    visible.value = false
    editing.value = null // 及时清掉，否则会持有 load() 之后已失效的行引用
    await load()
  } catch (e) {
    // 后端对成环、父分类不存在等情况都有明确 400 文案，直接透出
    ElMessage.error(e.message)
  }
}

async function remove(row) {
  // 有子分类时后端必定拒绝，这里先本地挡一次，省掉一次往返
  if (row.children && row.children.length) {
    return ElMessage.error(`「${row.name}」下还有 ${row.children.length} 个子分类，不能删除`)
  }
  try {
    await ElMessageBox.confirm(`确定删除分类「${row.name}」？`, '删除确认', { type: 'warning' })
  } catch {
    return // 用户取消
  }
  try {
    await api.deleteCategory(row.id)
    ElMessage.success('已删除')
    await load()
  } catch (e) {
    // 后端会说明「还有 N 个商品/子分类」，原样透出比自己猜更准
    ElMessage.error(e.message)
    await load()
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
.empty {
  color: #8b949e;
  font-size: 13px;
  padding: 24px 0;
  text-align: center;
}
.tip {
  color: #8b949e;
  font-size: 12px;
}
</style>
