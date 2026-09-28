import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import StatsView from '../views/StatsView.vue'
import { api } from '../api'

vi.mock('../api', () => ({
  api: {
    stats: vi.fn(),
    statsTrend: vi.fn(),
  },
}))

const OVERVIEW = {
  orders: { total: 7, by_status: { pending_payment: 2, completed: 5 }, paid_amount: 1234 },
  products: {
    total: 5,
    on_sale: 4,
    off_shelf: 1,
    sku_total: 6,
    stock_total: 100,
    low_stock: 2,
    low_stock_threshold: 10,
  },
  users: { active_total: 3 },
  recent: { days: 7, orders: 2, paid_amount: 500 },
}

beforeEach(() => {
  vi.clearAllMocks()
  api.stats.mockResolvedValue(OVERVIEW)
  api.statsTrend.mockResolvedValue({
    days: 7,
    items: [{ date: '2026-09-28', orders: 2, paid_amount: 500 }],
  })
})

describe('StatsView', () => {
  it('挂载后并发拉取概览与趋势', async () => {
    mount(StatsView)
    await flushPromises()
    expect(api.stats).toHaveBeenCalledTimes(1)
    expect(api.statsTrend).toHaveBeenCalledTimes(1)
  })

  it('把后端数据渲染成指标卡', async () => {
    const w = mount(StatsView)
    await flushPromises()
    const text = w.text()
    expect(text).toContain('订单总数')
    expect(text).toContain('销售额（GMV）')
    expect(text).toContain('低库存 SKU')
    // 金额格式化：去掉分隔符再比对，避免受 node 的 ICU locale 影响
    expect(text.replace(/[^\d.]/g, '')).toContain('1234.00')
  })

  it('GMV 副标题写明「仅统计已支付订单」（口径不能含糊）', async () => {
    const w = mount(StatsView)
    await flushPromises()
    expect(w.text()).toContain('仅统计已支付订单')
  })

  it('趋势表渲染出按天分桶的行', async () => {
    const w = mount(StatsView)
    await flushPromises()
    expect(w.text()).toContain('2026-09-28')
  })

  it('接口报错时提示，而不是白屏', async () => {
    api.stats.mockRejectedValue(new Error('boom'))
    const w = mount(StatsView)
    await flushPromises()
    // 组件仍应渲染出静态骨架（标题在），且没有把异常抛出去
    expect(w.text()).toContain('订单状态分布')
  })
})
