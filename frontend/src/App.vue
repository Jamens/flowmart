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
        <el-tab-pane label="流程设计器" name="designer">
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
import DesignerView from './views/DesignerView.vue'

// 登录态由后端 httpOnly Cookie 决定，前端不再持有明文令牌
const loggedIn = ref(false)
const active = ref('orders')

// 任意接口 401（Cookie 失效）时回到登录页
onMounted(async () => {
  setUnauthorizedHandler(() => {
    loggedIn.value = false
  })
  // 凭 Cookie 探活：已登录则直接进入后台
  try {
    await api.me()
    loggedIn.value = true
  } catch {
    loggedIn.value = false
  }
})

async function onLoggedIn() {
  // 登录接口已写入 httpOnly Cookie，无需前端存令牌；探活确认后进入后台
  try {
    await api.me()
    loggedIn.value = true
  } catch {
    loggedIn.value = false
  }
}

async function onLogout() {
  try {
    await api.logout()
  } catch {
    // 忽略：即便失败也强制回到登录页
  }
  loggedIn.value = false
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
