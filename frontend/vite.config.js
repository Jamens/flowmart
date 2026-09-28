import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // Vitest 配置：前端此前是**零测试**，只有 npm run build 能过——而构建通过 ≠ 行为正确。
  // environment 用 jsdom（组件要挂载），setupFiles 里注册 Element Plus（视图直接用全局组件）。
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.js'],
    include: ['src/**/*.spec.js'],
  },
  server: {
    // 显式绑定 IPv4：Vite 默认只监听 IPv6 的 ::1，
    // 会导致 127.0.0.1:5173 连不上（仅 localhost/[::1] 可访问）
    host: '127.0.0.1',
    port: 5173,
    // 开发期把 /api 代理到后端，避免跨域
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
