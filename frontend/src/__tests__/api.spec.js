import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api'

// api.js 直接调全局 fetch，这里把它换成 spy：
// 目的是验证「前端拼出来的请求长什么样」——这正是接口契约最容易悄悄走偏的地方。
beforeEach(() => {
  global.fetch = vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    json: () => Promise.resolve({}),
  })
})

describe('api 请求拼装', () => {
  it('stats 把查询参数拼进 URL', async () => {
    await api.stats({ low_stock_threshold: 5, recent_days: 30 })
    expect(global.fetch.mock.calls[0][0]).toBe(
      '/api/v1/stats?low_stock_threshold=5&recent_days=30'
    )
  })

  it('statsTrend 带上 days', async () => {
    await api.statsTrend(30)
    expect(global.fetch.mock.calls[0][0]).toBe('/api/v1/stats/trend?days=30')
  })

  it('每个请求都带 credentials: include（Cookie 鉴权不能被覆盖）', async () => {
    await api.stats()
    const opts = global.fetch.mock.calls[0][1]
    expect(opts.credentials).toBe('include')
  })
})
