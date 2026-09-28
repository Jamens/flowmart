import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'

// ESLint 9 的 flat config。
// 规则刻意从 `flat/essential` 起步（只含 Vue 模板里**真会出错**的规则），
// 而不是 recommended / all：老代码一上来就几十上百条，
// 结果要么大改、要么整段 disable——两种都比不开更糟。
export default [
  { ignores: ['dist/**', 'node_modules/**', 'coverage/**'] },
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.{js,vue}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: { ...globals.browser },
    },
    rules: {
      // 能拦住「拼错变量名 / 忘了 import」这类运行时才炸的问题
      'no-undef': 'error',
      // 未使用变量只警告：调试期留个变量很常见，但拼错的引用会暴露出来
      'no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
      // 视图组件名多为单词（App.vue、OrdersView.vue），这条会误报
      'vue/multi-word-component-names': 'off',
    },
  },
  {
    // 测试跑在 Node 下（Vitest），不是浏览器：global / process 等是可用的。
    // 只给测试文件开，避免业务代码误以为能直接用 Node 全局。
    files: ['src/**/*.spec.js', 'src/test/**/*.js'],
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
]
