import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { api } from '../api'
import OrdersView from '../views/OrdersView.vue'

// 注意：不要在 mount 里再挂一次 ElementPlus —— src/test/setup.js 已全局注册，
// 重复注册会刷「Plugin has already been applied」警告。

// 支付入口的可见性规则是「资金安全」的一部分：
// 管理员在订单页能看到全站订单，若给别人的待付款订单也显示「去支付」，
// 点了必然被后端 404 拒绝——等于把后端错误甩给用户（与本项目前端角色 gate 的取舍一致）。
vi.mock('../api', () => ({
  setUnauthorizedHandler: vi.fn(),
  api: {
    listOrders: vi.fn(),
    getOrder: vi.fn(),
    me: vi.fn(),
    listAllProducts: vi.fn(),
    listAddresses: vi.fn(),
    createPayment: vi.fn(),
    fireEvent: vi.fn(),
    createOrder: vi.fn(),
  },
}))

function orderRow(over = {}) {
  return {
    id: 1,
    order_no: 'NO-TEST-1',
    user_id: 7,
    status: 'pending_payment',
    pay_amount: 100,
    created_at: '2026-01-01T00:00:00',
    items: [{ sku_name: '测试商品', quantity: 1 }],
    ...over,
  }
}

async function mountView() {
  const wrapper = mount(OrdersView)
  await flushPromises()
  return wrapper
}

function payButtons(wrapper) {
  return wrapper.findAll('button').filter((b) => b.text() === '去支付')
}

beforeEach(() => {
  vi.clearAllMocks()
  api.me.mockResolvedValue({ id: 7, is_admin: false })
  api.listOrders.mockResolvedValue({ items: [orderRow()], total: 1 })
})

describe('订单页支付入口', () => {
  it('本人待付款订单显示「去支付」', async () => {
    const w = await mountView()
    expect(payButtons(w).length).toBe(1)
  })

  it('他人订单不显示「去支付」', async () => {
    api.listOrders.mockResolvedValue({ items: [orderRow({ user_id: 99 })], total: 1 })
    const w = await mountView()
    expect(payButtons(w).length).toBe(0)
  })

  it('非待付款订单不显示「去支付」', async () => {
    api.listOrders.mockResolvedValue({ items: [orderRow({ status: 'paid' })], total: 1 })
    const w = await mountView()
    expect(payButtons(w).length).toBe(0)
  })

  it('点击走 createPayment，绝不走 actions/pay（后者买家必然 403）', async () => {
    api.createPayment.mockResolvedValue({ channel: 'mock', paid: true })
    const w = await mountView()
    await payButtons(w)[0].trigger('click')
    await flushPromises()
    expect(api.createPayment).toHaveBeenCalledWith(1)
    expect(api.fireEvent).not.toHaveBeenCalled()
  })
})
