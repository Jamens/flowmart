const BASE = '/api/v1'
const TOKEN_KEY = 'flowmart_token'

export function getToken() {
  return localStorage.getItem(TOKEN_KEY) || ''
}
export function setToken(t) {
  localStorage.setItem(TOKEN_KEY, t)
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

// 未授权回调：由 App.vue 注入，用于 401 时跳回登录页
let unauthorizedHandler = null
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn
}

async function request(path, options = {}) {
  const token = getToken()
  const headers = { 'Content-Type': 'application/json', ...(options.headers || {}) }
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch(BASE + path, { headers, ...options })
  if (res.status === 401) {
    // 令牌失效/缺失：清掉并回到登录页，避免卡在错误态
    clearToken()
    if (unauthorizedHandler) unauthorizedHandler()
    throw new Error('登录已失效，请重新登录')
  }
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    const d = data.detail
    const msg =
      typeof d === 'string' ? d : d?.message ? `${d.message}：${(d.errors || []).join('；')}` : '请求失败'
    throw new Error(msg)
  }
  return data
}

export const api = {
  // 认证
  login: (username, password) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  register: (payload) =>
    request('/auth/register', { method: 'POST', body: JSON.stringify(payload) }),
  me: () => request('/auth/me'),

  // 订单
  listOrders: (status = '') => request(`/orders${status ? `?status=${status}` : ''}`),
  getOrder: (id) => request(`/orders/${id}`),
  createOrder: (payload) => request('/orders', { method: 'POST', body: JSON.stringify(payload) }),
  fireEvent: (id, event, payload) =>
    request(`/orders/${id}/actions/${event}`, {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }),

  // 商品
  listProducts: () => request('/products'),

  // 流程定义
  listDefinitions: () => request('/workflows/definitions'),
  getDefinition: (id) => request(`/workflows/definitions/${id}`),
  updateDefinition: (id, payload) =>
    request(`/workflows/definitions/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  createDefinition: (payload) =>
    request('/workflows/definitions', { method: 'POST', body: JSON.stringify(payload) }),
  publishDefinition: (id) => request(`/workflows/definitions/${id}/publish`, { method: 'POST' }),
}
