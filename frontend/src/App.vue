<template>
  <div>
    <header class="app-header">
      <h1>flowmart 管理后台</h1>
      <span class="sub">电商订单 + 可配置工作流引擎</span>
    </header>
    <el-tabs v-model="active" class="view" tab-position="top">
      <el-tab-pane label="订单管理" name="orders">
        <OrdersView />
      </el-tab-pane>
      <el-tab-pane label="流程设计器" name="designer">
        <DesignerView />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import OrdersView from './views/OrdersView.vue'
import DesignerView from './views/DesignerView.vue'

const active = ref('orders')
</script>
