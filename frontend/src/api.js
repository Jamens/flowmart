const BASE = '/api/v1'

// 未授权回调：由 App.vue 注入，用于 401 时跳回登录页
let unauthorizedHandler = null
export function setUnauthorizedHandler(fn) {
  unauthorizedHandler = fn
}

// 令牌由后端写入 httpOnly Cookie，浏览器随 credentials: 'include' 自动携带；
// 前端不再用 localStorage 存明文令牌，从根本上避免 XSS 窃令牌。
async function request(path, options = {}) {
  // 合并默认头与调用方传入的头；credentials:'include' 放在最后，确保 Cookie 鉴权永不被覆盖掉
  const res = await fetch(BASE + path, {
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    credentials: 'include',
  })
  if (res.status === 401) {
    // 令牌失效/缺失：回到登录页，避免卡在错误态（Cookie 由后端 /auth/logout 清除）
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
  logout: () => request('/auth/logout', { method: 'POST' }),

  // 订单
  listOrders: (status = '') => request(`/orders${status ? `?status=${status}` : ''}`),
  getOrder: (id) => request(`/orders/${id}`),
  createOrder: (payload) => request('/orders', { method: 'POST', body: JSON.stringify(payload) }),
  fireEvent: (id, event, payload) =>
    request(`/orders/${id}/actions/${event}`, {
      method: 'POST',
      body: JSON.stringify(payload || {}),
    }),

  // 购物车（归属由后端从令牌取，前端不传 user_id）
  getCart: () => request('/cart'),
  addToCart: (payload) =>
    request('/cart', { method: 'POST', body: JSON.stringify(payload) }),
  updateCartItem: (id, payload) =>
    request(`/cart/${id}`, { method: 'PATCH', body: JSON.stringify(payload) }),
  removeCartItem: (id) => request(`/cart/${id}`, { method: 'DELETE' }),
  // address_id 为 undefined 时 JSON.stringify 会自动省略该字段，即「不指定地址」
  checkout: (payload) =>
    request('/cart/checkout', { method: 'POST', body: JSON.stringify(payload || {}) }),

  // 商品
  listProducts: () => request('/products'),

  // 收货地址（必须走 /users/{id}/addresses：用户详情不返回地址，避免 PII 泄露）
  listAddresses: (userId) => request(`/users/${userId}/addresses`),

  // 流程定义
  listDefinitions: () => request('/workflows/definitions'),
  getDefinition: (id) => request(`/workflows/definitions/${id}`),
  updateDefinition: (id, payload) =>
    request(`/workflows/definitions/${id}`, { method: 'PUT', body: JSON.stringify(payload) }),
  createDefinition: (payload) =>
    request('/workflows/definitions', { method: 'POST', body: JSON.stringify(payload) }),
  publishDefinition: (id) => request(`/workflows/definitions/${id}/publish`, { method: 'POST' }),
}
