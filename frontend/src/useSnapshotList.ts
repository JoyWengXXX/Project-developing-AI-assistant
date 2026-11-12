import { useEffect, useMemo, useState } from 'react'
import { api, isAbortError, withQuery, type ApiError } from './api'
import { toApiError } from './format'
import type { Page, SnapshotItem } from './types'

const PAGE = 200 // 後端單頁上限
const MAX_ITEMS = 2_000 // 一個 repo 的快照超過這個數量就只載入前面的（新的在前）並提示

export interface SnapshotListState {
  /** 這個 repo＋篩選條件下，最近一次成功載入的全部快照（新的在前）；重新載入期間沿用舊資料，第一次載入前為 null。 */
  items: SnapshotItem[] | null
  /** 後端的 total 超過 MAX_ITEMS，只載入了一部分。 */
  truncated: boolean
  loading: boolean
  /** 最近一次載入的錯誤（有錯誤時 items 保留上一份資料）。 */
  error: ApiError | null
  /** 已匯入完成、且匯入設定是目前設定的快照，依 commit SHA 索引（「已匯入」的判斷與快照編號來源）。 */
  completedBySha: ReadonlyMap<string, SnapshotItem>
}

interface Settled {
  key: string // repo＋篩選＋reloadKey
  scope: string // repo＋篩選
  items: SnapshotItem[] | null
  truncated: boolean
  error: ApiError | null
}

/**
 * 讀取 repo 的快照列表（GET /repositories/{id}/snapshots，一頁 200 筆，載入全部頁）。
 * status：'' 代表全部；'completed' 等直接交給後端篩選。reloadKey 改變就重新載入。
 *
 * 與 useLoad 的差別：只換 reloadKey（例如匯入、移除、設定參考之後）時，先沿用上一份資料，
 * 畫面不會閃成「沒有快照」；換 repo 或換篩選則立刻清空。唯讀，不會建立或匯入任何東西。
 */
export function useSnapshotList(repoId: number, status: string, reloadKey: number): SnapshotListState {
  const key = `${repoId}:${status}:${reloadKey}`
  const scope = `${repoId}:${status}`
  const [settled, setSettled] = useState<Settled | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    async function loadAll() {
      const items: SnapshotItem[] = []
      let total = 0
      do {
        const path = withQuery(`/api/v1/repositories/${repoId}/snapshots`, {
          status,
          limit: PAGE,
          offset: items.length,
        })
        const res = await api<Page<SnapshotItem>>('GET', path, { signal: controller.signal })
        total = res.data.total
        items.push(...res.data.items)
        if (res.data.items.length === 0) break
      } while (items.length < total && items.length < MAX_ITEMS)
      return { items, truncated: total > items.length }
    }
    loadAll().then(
      (r) => setSettled({ key, scope, items: r.items, truncated: r.truncated, error: null }),
      (e: unknown) => {
        if (isAbortError(e)) return
        // 失敗：保留同一範圍的上一份資料（若有）
        setSettled((prev) => ({
          key,
          scope,
          items: prev !== null && prev.scope === scope ? prev.items : null,
          truncated: prev !== null && prev.scope === scope ? prev.truncated : false,
          error: toApiError(e),
        }))
      },
    )
    return () => controller.abort()
  }, [key, scope, repoId, status])

  const current = settled !== null && settled.scope === scope ? settled : null
  const items = current?.items ?? null
  const completedBySha = useMemo(() => {
    const map = new Map<string, SnapshotItem>()
    for (const s of items ?? []) {
      if (s.status === 'completed' && !s.is_stale_profile) map.set(s.commit_sha, s)
    }
    return map
  }, [items])

  return {
    items,
    truncated: current?.truncated ?? false,
    loading: current?.key !== key,
    error: current?.key === key ? current.error : null,
    completedBySha,
  }
}
