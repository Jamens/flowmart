import { config } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// 视图里直接用 <el-card> / <el-table> 等全局组件（main.js 里 app.use(ElementPlus) 注册），
// 测试环境没有那次注册，必须显式装上，否则组件渲染不出来、断言全落空。
config.global.plugins = [ElementPlus]
