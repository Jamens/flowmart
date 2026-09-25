<template>
  <div>
    <LoginView v-if="!loggedIn" @logged-in="onLoggedIn" />
    <template v-else>
      <header class="app-header">
        <h1>flowmart 管理后台</h1>
        <span class="sub">电商订单 + 可配置工作流引擎</span>
        <el-button class="logout" text type="primary" @click="onLogout">退出登录</el-button>
      </header>
      <el-tabs v-model="active" class="view" tab-position="top">
        <el-tab-pane label="订单管理" name="orders">
          <OrdersView />
        </el-tab-pane>
        <el-tab-pane label="购物车" name="cart">
          <CartView />
        </el-tab-pane>
        <el-tab-pane label="商品管理" name="products">
          <ProductsView :is-admin="isAdmin" />
        </el-tab-pane>
        <!-- 分类与流程定义的写操作已收紧为 require_admin，买家进来只能看不能改，
             且改不了任何东西的页面没有意义，整块隐藏 -->
        <el-tab-pane v-if="isAdmin" label="分类管理" name="categories">
          <CategoriesView />
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" label="用户管理" name="users">
          <UsersView />
        </el-tab-pane>
        <el-tab-pane v-if="isAdmin" label="流程设计器" name="designer">
          <DesignerView />
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { api, setUnauthorizedHandler } from './api.js'
import LoginView from './views/LoginView.vue'
import OrdersView from './views/OrdersView.vue'
import CartView from './views/CartView.vue'
import ProductsView from './views/ProductsView.vue'
import CategoriesView from './views/CategoriesView.vue'
import UsersView from './views/UsersView.vue'
import DesignerView from './views/DesignerView.vue'

// 登录态由后端 httpOnly Cookie 决定，前端不再持有明文令牌
const loggedIn = ref(false)
const active = ref('orders')
// 角色用于隐藏买家无权访问的入口。后端对管理员接口是硬 403（users / admin_db /
// 商品写操作），但前端若不做隐藏，买家点每个按钮都会弹「需要管理员权限」，
// 等于把后端错误直接甩给用户。角色只影响**显示**，真正的校验永远在后端。
const isAdmin = ref(false)
// 仅管理员可见的 tab（对应后端 require_admin 的写操作）。
// 与模板里的 v-if 保持一致，避免角色变化后停在已隐藏的 tab 上出现空白。
const ADMIN_ONLY_TABS = ['categories', 'users', 'designer']

async function probe() {
  // 凭 Cookie 探活：已登录则直接进入后台，并取回角色
  try {
    const me = await api.me()
    loggedIn.value = true
    isAdmin.value = !!me.is_admin
  } catch {
    loggedIn.value = false
    isAdmin.value = false
  }
  // 角色变化后若停在已被隐藏的 tab，内容区会空白且无提示，兜回订单页
  if (!isAdmin.value && ADMIN_ONLY_TABS.includes(active.value)) active.value = 'orders'
}

// 任意接口 401（Cookie 失效）时回到登录页
onMounted(async () => {
  setUnauthorizedHandler(() => {
    loggedIn.value = false
    isAdmin.value = false
  })
  await probe()
})

async function onLoggedIn() {
  // 登录接口已写入 httpOnly Cookie，无需前端存令牌；探活确认后进入后台
  await probe()
}

async function onLogout() {
  try {
    await api.logout()
  } catch {
    // 忽略：即便失败也强制回到登录页
  }
  loggedIn.value = false
  isAdmin.value = false
  active.value = 'orders'
}
</script>

<style scoped>
.app-header {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 12px 20px;
  border-bottom: 1px solid var(--el-border-color);
}
.app-header h1 {
  font-size: 18px;
  margin: 0;
}
.sub {
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.logout {
  margin-left: auto;
}
</style>
