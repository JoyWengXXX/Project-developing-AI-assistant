import { useEffect, useState } from 'react'
import { api, isAbortError, withQuery, type ApiError } from './api'
import { toApiError } from './format'
import type { Page, SnapshotFileSummary } from './types'

/** 後端單頁上限（MAX_PAGE_LIMIT）。 */
const PAGE_LIMIT = 200
/** 最多載入的檔案數；與後端匯入檔案數預設上限（import_max_files = 20,000）一致。 */
export const MAX_FILES = 20_000

export interface AllFilesState {
  files: SnapshotFileSummary[] | null
  /** 符合條件的總數（可能大於 files.length，見 truncated）。 */
  total: number
  /** 載入中的進度；載入完成後等於 files.length。 */
  loaded: number
  /** 因超過 MAX_FILES 而只載入前 MAX_FILES 筆。 */
  truncated: boolean
  error: ApiError | null
  loading: boolean
}

interface Settled {
  key: string
  files: SnapshotFileSummary[] | null
  total: number
  truncated: boolean
  error: ApiError | null
}

interface Progress {
  key: string
  loaded: number
  total: number
}

/**
 * 逐頁（limit=200）載入快照的全部檔案，供樹狀圖使用。
 * 做法與 useLoad 相同：
 * - snapshotId、status 或 reloadKey 改變／元件卸載時，取消進行中的請求（AbortController）。
 * - 結果與進度都記錄自己屬於哪一次載入（key），不符的一律視為過期，不會覆蓋新狀態。
 * - 頁與頁之間是依序 await，不會一次送出上百個請求；只在每頁完成時更新進度，
 *   整批完成才一次更新 files，所以樹只會建一次。
 * 任何一頁失敗（例如快照非 completed 的 409）就整批視為失敗，並顯示後端的錯誤。
 */
export function useAllFiles(snapshotId: number, status: string, reloadKey: number): AllFilesState {
  const key = `${reloadKey}:${snapshotId}:${status}`
  const [settled, setSettled] = useState<Settled | null>(null)
  const [progress, setProgress] = useState<Progress | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    const basePath = `/api/v1/snapshots/${snapshotId}/files`

    async function loadAll() {
      const all: SnapshotFileSummary[] = []
      let total = 0
      let truncated = false
      while (true) {
        const path = withQuery(basePath, { status, limit: PAGE_LIMIT, offset: all.length })
        const { data } = await api<Page<SnapshotFileSummary>>('GET', path, { signal: controller.signal })
        total = data.total
        all.push(...data.items)
        setProgress({ key, loaded: all.length, total })
        if (data.items.length === 0 || all.length >= total) break
        if (all.length >= MAX_FILES) {
          truncated = true
          break
        }
      }
      return { all, total, truncated }
    }

    loadAll().then(
      ({ all, total, truncated }) => setSettled({ key, files: all, total, truncated, error: null }),
      (e: unknown) => {
        if (isAbortError(e)) return
        setSettled({ key, files: null, total: 0, truncated: false, error: toApiError(e) })
      },
    )
    return () => controller.abort()
  }, [snapshotId, status, key])

  const current = settled !== null && settled.key === key ? settled : null
  const currentProgress = progress !== null && progress.key === key ? progress : null
  return {
    files: current?.files ?? null,
    total: current?.total ?? currentProgress?.total ?? 0,
    loaded: current?.files?.length ?? currentProgress?.loaded ?? 0,
    truncated: current?.truncated ?? false,
    error: current?.error ?? null,
    loading: current === null,
  }
}
