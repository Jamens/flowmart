import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// 暗色主题：与 IDE 主题保持一致
import 'element-plus/theme-chalk/dark/css-vars.css'
import './style.css'
import App from './App.vue'

createApp(App).use(ElementPlus).mount('#app')
