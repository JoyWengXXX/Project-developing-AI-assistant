import { useEffect, useState } from 'react'
import { api, isAbortError, type ApiError } from './api'
import { toApiError } from './format'

interface LoadState<T> {
  data: T | null
  error: ApiError | null
  loading: boolean
}

interface Settled<T> {
  key: string // 這筆結果是哪一次載入（reloadKey＋path）的
  data: T | null
  error: ApiError | null
}

/**
 * 對 GET 路徑載入資料。
 * - path 為 null：不呼叫（尚未選取）。
 * - path 或 reloadKey 改變：取消舊請求並重新載入。
 * - 結果會記下自己屬於哪一次載入；與目前載入不符的舊結果一律視為「載入中」，
 *   所以不會顯示上一個 repo／快照的過期資料，慢回來的舊回應也不會覆蓋新狀態。
 */
export function useLoad<T>(path: string | null, reloadKey = 0): LoadState<T> {
  const key = path === null ? null : `${reloadKey}:${path}`
  const [settled, setSettled] = useState<Settled<T> | null>(null)

  useEffect(() => {
    if (path === null || key === null) return
    const controller = new AbortController()
    api<T>('GET', path, { signal: controller.signal }).then(
      (result) => setSettled({ key, data: result.data, error: null }),
      (e: unknown) => {
        if (isAbortError(e)) return
        setSettled({ key, data: null, error: toApiError(e) })
      },
    )
    return () => controller.abort()
  }, [path, key])

  const current = settled !== null && settled.key === key ? settled : null
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: key !== null && current === null,
  }
}
