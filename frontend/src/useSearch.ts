import { useCallback, useEffect, useRef, useState } from 'react'
import { api, isAbortError, type ApiError } from './api'
import { toApiError } from './format'
import { nextOffset, pagePath } from './search'
import type { SearchPageBase } from './types'

interface Settled<T> {
  key: string // 這筆結果是哪一次搜尋（含篩選條件的 basePath）的
  items: T[]
  total: number | null
  hasMore: boolean
  next: number // 下一頁的 offset
  error: ApiError | null // 第一頁失敗
  moreError: ApiError | null // 「載入更多」失敗（已載入的結果保留）
  moreLoading: boolean
}

export interface SearchState<T> {
  items: T[]
  total: number | null
  hasMore: boolean
  next: number
  loading: boolean // 第一頁載入中
  loadingMore: boolean
  error: ApiError | null
  moreError: ApiError | null
  loadMore: () => void
}

/**
 * 搜尋結果的載入與「載入更多」。
 * - basePath 為 null：不搜尋（關鍵字不合法或還沒輸入）。
 * - basePath 改變（關鍵字、篩選、分頁籤或快照）：取消進行中的請求並從第一頁重新搜尋；
 *   結果記下自己屬於哪個 basePath，與目前不符的舊結果一律視為「載入中」而不顯示，
 *   所以慢回來的舊回應不會覆蓋新狀態，也不會把舊關鍵字的命中混進新的清單。
 * - 「載入更多」以回應的 offset＋實際筆數為下一頁，結果附加在後面；同一時間只有一個進行中。
 */
export function useSearch<T>(basePath: string | null): SearchState<T> {
  const [state, setState] = useState<Settled<T> | null>(null)
  const moreController = useRef<AbortController | null>(null)

  useEffect(() => {
    if (basePath === null) return
    const controller = new AbortController()
    api<SearchPageBase<T>>('GET', pagePath(basePath, 0), { signal: controller.signal }).then(
      (result) => {
        const page = result.data
        setState({
          key: basePath,
          items: page.items,
          total: page.total,
          hasMore: page.has_more,
          next: nextOffset(page),
          error: null,
          moreError: null,
          moreLoading: false,
        })
      },
      (e: unknown) => {
        if (isAbortError(e)) return
        setState({ key: basePath, items: [], total: null, hasMore: false, next: 0, error: toApiError(e), moreError: null, moreLoading: false })
      },
    )
    return () => {
      controller.abort()
      moreController.current?.abort()
      moreController.current = null
    }
  }, [basePath])

  const current = state !== null && state.key === basePath ? state : null
  const canLoadMore = basePath !== null && current !== null && current.error === null && current.hasMore && !current.moreLoading
  const requestOffset = current?.next ?? 0

  const loadMore = useCallback(() => {
    if (basePath === null || !canLoadMore) return
    const controller = new AbortController()
    moreController.current = controller
    const mark = (patch: Partial<Settled<T>>) =>
      setState((prev) => (prev !== null && prev.key === basePath ? { ...prev, ...patch } : prev))
    mark({ moreLoading: true, moreError: null })
    api<SearchPageBase<T>>('GET', pagePath(basePath, requestOffset), { signal: controller.signal }).then(
      (result) => {
        const page = result.data
        setState((prev) => {
          // 只接受「正好接在目前清單後面」的那一頁，避免重複附加
          if (prev === null || prev.key !== basePath || prev.next !== requestOffset) return prev
          return {
            ...prev,
            items: [...prev.items, ...page.items],
            total: page.total,
            hasMore: page.has_more,
            next: nextOffset(page),
            moreError: null,
            moreLoading: false,
          }
        })
      },
      (e: unknown) => {
        if (isAbortError(e)) return
        mark({ moreError: toApiError(e), moreLoading: false })
      },
    )
  }, [basePath, canLoadMore, requestOffset])

  return {
    items: current?.items ?? [],
    total: current?.total ?? null,
    hasMore: current?.hasMore ?? false,
    next: current?.next ?? 0,
    loading: basePath !== null && current === null,
    loadingMore: current?.moreLoading ?? false,
    error: current?.error ?? null,
    moreError: current?.moreError ?? null,
    loadMore,
  }
}

/** 輸入值穩定 delayMs 毫秒後才更新的值（用來延遲送出搜尋）。 */
export function useDebounced<T>(value: T, delayMs: number): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delayMs)
    return () => window.clearTimeout(timer)
  }, [value, delayMs])
  return debounced
}
