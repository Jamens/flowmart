<template>
  <div class="login-wrap">
    <el-card class="login-card">
      <h2>flowmart 管理后台</h2>
      <p class="tip">登录后所有请求自动携带令牌</p>
      <el-form @submit.prevent="onSubmit">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="password" type="password" placeholder="密码" size="large" show-password />
        </el-form-item>
        <el-button type="primary" size="large" :loading="loading" @click="onSubmit" style="width: 100%">
          登录
        </el-button>
      </el-form>
      <p class="hint">演示账号：zhangsan / 123456（密码由种子脚本写入）</p>
      <p v-if="error" class="error">{{ error }}</p>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const emit = defineEmits(['logged-in'])

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function onSubmit() {
  if (!username.value || !password.value) {
    error.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  error.value = ''
  try {
    await api.login(username.value, password.value)
    // 后端已将 JWT 写入 httpOnly Cookie，浏览器自动携带；无需前端存令牌
    emit('logged-in')
  } catch (e) {
    error.value = e.message || '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-wrap {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-bg-color-page, #141414);
}
.login-card {
  width: 360px;
  padding: 8px 12px;
}
.tip {
  color: var(--el-text-color-secondary);
  margin-top: -8px;
}
.hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-top: 12px;
}
.error {
  color: var(--el-color-danger);
  margin-top: 8px;
}
</style>
