<template>
  <div>
    <div class="toolbar">
      <!-- 切换「只看启用中」等于换筛选条件，必须回到第一页，否则会停在越界空页 -->
      <el-checkbox v-model="activeOnly" @change="onFilterChange">只看启用中</el-checkbox>
      <el-button @click="load">刷新</el-button>
      <el-button type="primary" @click="openCreate">新建用户</el-button>
      <!-- 地址管理对所有人开放（后端允许管理自己的地址），
           不能只挂在用户行上 —— 列表本身仅管理员可见，否则普通用户永远进不去 -->
      <el-button :disabled="!myId" @click="openAddresses({ id: myId })">我的收货地址</el-button>
    </div>

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

    <el-table :data="list" stripe v-loading="loading">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="username" label="用户名" width="150" />
      <el-table-column prop="nickname" label="昵称" width="150" />
      <el-table-column prop="phone" label="手机号" width="140" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'">
            {{ row.is_active ? '启用' : '已禁用' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="230">
        <template #default="{ row }">
          <el-button size="small" @click="openEdit(row)">编辑</el-button>
          <!-- 可反向操作：后端允许管理员重新启用，误禁用必须能恢复 -->
          <el-button
            size="small"
            :type="row.is_active ? 'warning' : 'success'"
            :disabled="row.id === myId"
            @click="toggleActive(row)"
          >
            {{ row.is_active ? '禁用' : '启用' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <p class="tip">
      禁用是软删除（置 <code>is_active=False</code>）：订单仍引用该用户，物理删除会破坏历史订单。
      不能禁用当前登录的管理员（后端会拒绝，避免把自己锁死）。
    </p>

    <!-- 新建 / 编辑用户 -->
    <el-dialog v-model="userVisible" :title="editing ? '编辑用户' : '新建用户'" width="460px">
      <el-form label-width="80px">
        <template v-if="!editing">
          <el-form-item label="用户名" required>
            <el-input v-model="userForm.username" maxlength="64" placeholder="必填，唯一" />
          </el-form-item>
          <el-form-item label="初始密码">
            <el-input
              v-model="userForm.password"
              type="password"
              show-password
              maxlength="128"
              placeholder="留空则不设密码"
            />
          </el-form-item>
        </template>
        <el-form-item label="昵称">
          <el-input v-model="userForm.nickname" />
        </el-form-item>
        <el-form-item label="手机号">
          <el-input v-model="userForm.phone" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userVisible = false">取消</el-button>
        <el-button type="primary" @click="submitUser">提交</el-button>
      </template>
    </el-dialog>

    <!-- 收货地址（仅本人） -->
    <el-drawer v-model="addrVisible" title="我的收货地址" size="46%">
      <el-button size="small" type="primary" @click="openAddrForm(null)">新增地址</el-button>
      <el-table :data="addresses" size="small" class="addr-table">
        <el-table-column prop="receiver" label="收货人" width="100" />
        <el-table-column prop="phone" label="电话" width="130" />
        <el-table-column label="地址">
          <template #default="{ row }">
            {{ row.province }}{{ row.city }}{{ row.district }}{{ row.detail }}
            <el-tag v-if="row.is_default" size="small" type="success">默认</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-button size="small" @click="openAddrForm(row)">编辑</el-button>
            <el-button size="small" type="danger" text @click="removeAddr(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!addresses.length" class="tip">还没有收货地址</div>

      <el-dialog v-model="addrFormVisible" title="收货地址" width="460px" append-to-body>
        <el-form label-width="80px">
          <el-form-item label="收货人" required>
            <el-input v-model="addrForm.receiver" maxlength="64" />
          </el-form-item>
          <el-form-item label="电话" required>
            <el-input v-model="addrForm.phone" maxlength="20" />
          </el-form-item>
          <el-form-item label="省市区">
            <el-input v-model="addrForm.province" placeholder="省" style="width: 30%" />
            <el-input v-model="addrForm.city" placeholder="市" style="width: 30%; margin: 0 5%" />
            <el-input v-model="addrForm.district" placeholder="区" style="width: 30%" />
          </el-form-item>
          <el-form-item label="详细地址">
            <el-input v-model="addrForm.detail" type="textarea" :rows="2" />
          </el-form-item>
          <el-form-item label="默认">
            <el-switch v-model="addrForm.is_default" />
          </el-form-item>
        </el-form>
        <template #footer>
          <el-button @click="addrFormVisible = false">取消</el-button>
          <el-button type="primary" @click="submitAddr">提交</el-button>
        </template>
      </el-dialog>
    </el-drawer>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '../api'

const list = ref([])
const loading = ref(false)
const activeOnly = ref(false)
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const myId = ref(null)

const userVisible = ref(false)
const editing = ref(null)
const userForm = ref({ username: '', password: '', nickname: '', phone: '' })

const addrVisible = ref(false)
const addrFormVisible = ref(false)
const addresses = ref([])
const addrEditing = ref(null)
const addrForm = ref(emptyAddr())

function emptyAddr() {
  return {
    receiver: '',
    phone: '',
    province: '',
    city: '',
    district: '',
    detail: '',
    is_default: false,
  }
}

async function load() {
  loading.value = true
  try {
    // 列表接口返回 {items, total}：total 是过滤后的总数，与当前页无关
    const res = await api.listUsers({
      active_only: activeOnly.value,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    list.value = res.items
    total.value = res.total
  } catch (e) {
    // 非管理员会被 403，如实提示而不是静默空列表
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

function onFilterChange() {
  page.value = 1 // 换筛选条件回到第一页，避免停在越界空页
  load()
}

function onSizeChange() {
  page.value = 1
  load()
}

async function loadMe() {
  try {
    const me = await api.me()
    myId.value = me.id
  } catch (e) {
    // 拿不到自己的 id 就等于整个地址管理不可用，必须让用户看见原因
    myId.value = null
    ElMessage.error(e.message)
  }
}

function openCreate() {
  editing.value = null
  userForm.value = { username: '', password: '', nickname: '', phone: '' }
  userVisible.value = true
}

function openEdit(row) {
  editing.value = row
  userForm.value = { username: row.username, password: '', nickname: row.nickname || '', phone: row.phone || '' }
  userVisible.value = true
}

async function submitUser() {
  const f = userForm.value
  try {
    if (editing.value) {
      // 后端只允许改 nickname / phone（管理员还可改 is_active），这里不传多余字段
      await api.updateUser(editing.value.id, { nickname: f.nickname, phone: f.phone })
      ElMessage.success('已更新')
    } else {
      if (!f.username.trim()) return ElMessage.warning('请填写用户名')
      await api.createUser({
        username: f.username.trim(),
        password: f.password,
        nickname: f.nickname,
        phone: f.phone,
      })
      ElMessage.success('已创建')
    }
    userVisible.value = false
    editing.value = null
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function toggleActive(row) {
  if (!row.is_active) {
    // 重新启用：后端允许管理员置 is_active=True（改资料接口）
    try {
      await api.updateUser(row.id, { is_active: true })
      ElMessage.success('已启用')
      await load()
    } catch (e) {
      ElMessage.error(e.message)
    }
    return
  }
  try {
    await ElMessageBox.confirm(
      `确定禁用用户「${row.username}」？该操作为软删除，历史订单保留。`,
      '禁用确认',
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await api.disableUser(row.id)
    ElMessage.success('已禁用')
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function openAddresses(row) {
  // 关掉可能残留的内层弹窗，否则再次打开抽屉会直接弹出地址表单
  addrFormVisible.value = false
  addrVisible.value = true
  await loadAddresses(row.id)
}

async function loadAddresses(userId) {
  try {
    addresses.value = await api.listAddresses(userId)
  } catch (e) {
    ElMessage.error(e.message)
    addresses.value = []
  }
}

function openAddrForm(row) {
  addrEditing.value = row
  addrForm.value = row
    ? { ...row }
    : emptyAddr()
  addrFormVisible.value = true
}

async function submitAddr() {
  const f = addrForm.value
  if (!f.receiver.trim()) return ElMessage.warning('请填写收货人')
  if (!f.phone.trim()) return ElMessage.warning('请填写电话')
  // 显式列出字段：addrForm 来自 {...row} 会带上 id，虽然 Pydantic 会忽略，
  // 但把主键塞进请求体是坏习惯
  const payload = {
    receiver: f.receiver,
    phone: f.phone,
    province: f.province,
    city: f.city,
    district: f.district,
    detail: f.detail,
    is_default: f.is_default,
  }
  try {
    if (addrEditing.value) {
      await api.updateAddress(myId.value, addrEditing.value.id, payload)
    } else {
      await api.createAddress(myId.value, payload)
    }
    ElMessage.success('已保存')
    addrFormVisible.value = false
    await loadAddresses(myId.value)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function removeAddr(row) {
  try {
    await api.deleteAddress(myId.value, row.id)
    ElMessage.success('已删除')
    await loadAddresses(myId.value)
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(async () => {
  await Promise.all([load(), loadMe()])
})
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}
.pager {
  margin: 14px 0;
  justify-content: flex-end;
}
.addr-table {
  margin-top: 12px;
}
.tip {
  color: #8b949e;
  font-size: 12px;
  margin: 10px 0 0;
}
code {
  background: #1c2230;
  padding: 1px 5px;
  border-radius: 3px;
}
</style>
