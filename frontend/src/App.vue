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
        <el-tab-pane label="流程设计器" name="designer">
          <DesignerView />
        </el-tab-pane>
      </el-tabs>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { api, getToken, setToken, clearToken, setUnauthorizedHandler } from './api.js'
import LoginView from './views/LoginView.vue'
import OrdersView from './views/OrdersView.vue'
import DesignerView from './views/DesignerView.vue'

const token = ref(getToken())
const loggedIn = computed(() => !!token.value)
const active = ref('orders')

// 任意接口 401（令牌失效）时回到登录页
onMounted(() => {
  setUnauthorizedHandler(() => {
    token.value = ''
  })
})

function onLoggedIn(t) {
  setToken(t)
  token.value = t
}

function onLogout() {
  clearToken()
  token.value = ''
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
