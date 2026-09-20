import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
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
