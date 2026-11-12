import { useEffect, useRef, useState } from 'react'
import { api, isAbortError, withQuery, type ApiError } from './api'
import { toApiError } from './format'
import type { Commit, CommitList } from './types'

/** 每頁筆數（後端預設 100、上限 500）。 */
export const COMMIT_PAGE_SIZE = 100
const MAX_COMMIT_LIMIT = 500 // 後端單頁上限

function commitsPath(repoId: number, ref: string | null | undefined, offset: number, limit = COMMIT_PAGE_SIZE): string {
  return withQuery(`/api/v1/repositories/${repoId}/commits`, {
    ref: ref ?? undefined, // null（所有分支）就不帶 ref 參數
    limit,
    offset,
  })
}

interface Settled {
  key: string // 這筆結果是哪一次載入的（repo＋ref＋reloadKey）
  scope: string // 哪個範圍的（repo＋ref，不含 reloadKey）：只換 reloadKey 重新載入時，舊結果可先沿用
  items: Commit[]
  hasMore: boolean
  error: ApiError | null
}

interface MoreState {
  key: string
  loading: boolean
  error: ApiError | null
}

export interface CommitsState {
  /** 目前範圍的 commit（依 topo 順序）；第一頁載入中或失敗時為 null。 */
  items: Commit[] | null
  hasMore: boolean
  error: ApiError | null
  /** 第一頁載入中（含「範圍已決定但請求尚未回來」）。 */
  loading: boolean
  loadingMore: boolean
  /** 「載入更多」失敗的錯誤；已載入的 commit 與圖保留不動。 */
  moreError: ApiError | null
  loadMore: () => void
}

/**
 * 載入 repo 的 commit 歷史，支援「載入更多」（以 offset 追加）。
 * ref：undefined＝尚未決定（不發請求）、null＝所有分支、字串＝完整 ref 名稱（/refs 回傳的 ref）。
 *
 * 與 useLoad 同一套做法：repo、ref 或 reloadKey 改變時取消進行中的請求（含「載入更多」），
 * 每筆結果都記下自己屬於哪一次載入（key），不符的視為過期，所以慢回來的舊回應不會覆蓋新狀態。
 * 差別：只有 reloadKey 改變（同一個 repo 與範圍）時，新結果回來之前先沿用上一份（loading 為 true），
 * 圖不會閃掉——匯入或移除版本後重新讀取 imported 標記就是這種情況；換 repo 或換範圍仍然立刻清空。
 */
export function useCommits(repoId: number, ref: string | null | undefined, reloadKey: number): CommitsState {
  const key = ref === undefined ? null : `${reloadKey}:${repoId}:${ref ?? '*'}`
  const scope = ref === undefined ? null : `${repoId}:${ref ?? '*'}`
  const [settled, setSettled] = useState<Settled | null>(null)
  const [more, setMore] = useState<MoreState | null>(null)
  const moreController = useRef<AbortController | null>(null)
  // 同一範圍上一次顯示了幾筆：重新載入時一次要回同樣多（上限 500），圖不會縮回第一頁
  const shown = useRef({ scope: '', count: 0 })

  // 只依 key 重新載入：repoId、ref、reloadKey 都已包含在 key 裡
  useEffect(() => {
    if (key === null || scope === null) return
    const controller = new AbortController()
    const limit = shown.current.scope === scope ? Math.min(MAX_COMMIT_LIMIT, Math.max(COMMIT_PAGE_SIZE, shown.current.count)) : COMMIT_PAGE_SIZE
    api<CommitList>('GET', commitsPath(repoId, ref, 0, limit), { signal: controller.signal }).then(
      (res) => setSettled({ key, scope, items: res.data.items, hasMore: res.data.has_more, error: null }),
      (e: unknown) => {
        if (isAbortError(e)) return
        setSettled({ key, scope, items: [], hasMore: false, error: toApiError(e) })
      },
    )
    return () => {
      controller.abort()
      moreController.current?.abort() // 範圍改變時，進行中的「載入更多」也一併取消
    }
  }, [key, scope, repoId, ref])

  // 同一次載入的結果；或同一範圍上一次「成功」的結果（重新載入期間沿用）
  const exact = settled !== null && settled.key === key ? settled : null
  const current =
    exact ?? (settled !== null && scope !== null && settled.scope === scope && settled.error === null ? settled : null)
  const currentMore = more !== null && more.key === key ? more : null
  useEffect(() => {
    if (current !== null && current.error === null) shown.current = { scope: current.scope, count: current.items.length }
  })

  function loadMore() {
    if (key === null || current === null || exact === null || current.error !== null || !current.hasMore) return
    if (currentMore?.loading) return // 已在載入：不重複送出
    const controller = new AbortController()
    moreController.current = controller
    const requestKey = key
    setMore({ key: requestKey, loading: true, error: null })
    api<CommitList>('GET', commitsPath(repoId, ref, current.items.length), { signal: controller.signal }).then(
      (res) => {
        setSettled((prev) => {
          if (prev === null || prev.key !== requestKey) return prev // 範圍已改變：丟棄
          const known = new Set(prev.items.map((c) => c.sha))
          const fresh = res.data.items.filter((c) => !known.has(c.sha)) // 分頁之間 refs 若變動，避免重複列
          return { ...prev, items: [...prev.items, ...fresh], hasMore: res.data.has_more }
        })
        setMore((prev) => (prev?.key === requestKey ? null : prev))
      },
      (e: unknown) => {
        if (isAbortError(e)) return
        setMore((prev) => (prev?.key === requestKey ? { key: requestKey, loading: false, error: toApiError(e) } : prev))
      },
    )
  }

  return {
    items: current !== null && current.error === null ? current.items : null,
    hasMore: current?.hasMore ?? false,
    error: exact?.error ?? null,
    loading: key !== null && exact === null,
    loadingMore: currentMore?.loading ?? false,
    moreError: currentMore?.error ?? null,
    loadMore,
  }
}
