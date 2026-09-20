const BASE = '/api/v1'

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) {
    // 后端校验失败时 detail 可能是字符串，也可能是 {message, errors[]}
    const d = data.detail
    const msg =
      typeof d === 'string' ? d : d?.message ? `${d.message}：${(d.errors || []).join('；')}` : '请求失败'
    throw new Error(msg)
  }
  return data
}

export const api = {
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
