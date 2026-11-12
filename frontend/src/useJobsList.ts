import { withQuery } from './api'
import { JOBS_POLL_FAST_MS, JOBS_POLL_SLOW_MS } from './useJobsOverview'
import type { Job, Page } from './types'
import { usePolling, type PollState } from './usePolling'

export interface JobsListQuery {
  status: string // '' 代表不篩選；後端的篩選值（active、failed、succeeded、cancelled…）
  kind: string // '' 代表不篩選
  limit: number
  offset: number
}

/**
 * 完整列表在「進行中與失敗」以外的篩選：向後端分頁取一頁（GET /jobs?status=&kind=&limit=&offset=）。
 * 只在 enabled（對話框開啟且不是「進行中與失敗」）時輪詢；對話框關閉時完全不載入列表資料。
 * busy：頁面共用的輪詢知道有進行中的工作時，這一頁也用較密的間隔；reloadKey 改變就立刻重抓。
 */
export function useJobsList(query: JobsListQuery, enabled: boolean, busy: boolean, reloadKey: string): PollState<Page<Job>> {
  return usePolling<Page<Job>>(withQuery('/api/v1/jobs', { ...query }), {
    enabled,
    reloadKey,
    intervalMs: (data) =>
      busy || (data !== null && data.items.some((j) => j.status === 'queued' || j.status === 'running'))
        ? JOBS_POLL_FAST_MS
        : JOBS_POLL_SLOW_MS,
  })
}
