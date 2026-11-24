import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, isAbortError, type ApiError } from './api'
import { toApiError } from './format'
import type { ReferenceItem, ReferenceList, ReferenceSetResult } from './types'

export interface ReferencesState {
  /** 最近一次成功載入的參考版本（每個 repo 最多一個）；重新載入期間沿用舊資料，第一次載入前為 null。 */
  items: ReferenceItem[] | null
  byRepo: ReadonlyMap<number, ReferenceItem>
  loading: boolean
  error: ApiError | null
  /** 重新讀取（設定／取消參考、匯入、移除版本或 repo 之後）。 */
  reload: () => void
}

/**
 * 參考版本清單（GET /references；資料庫是唯一來源，取代舊的 localStorage 清單）。
 * 參考只是標記，尚未連到 Agent。
 */
export function useReferences(): ReferencesState {
  const [reloadKey, setReloadKey] = useState(0)
  const [settled, setSettled] = useState<{ key: number; items: ReferenceItem[] | null; error: ApiError | null } | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    api<ReferenceList>('GET', '/api/v1/references', { signal: controller.signal }).then(
      (res) => setSettled({ key: reloadKey, items: res.data.items, error: null }),
      (e: unknown) => {
        if (isAbortError(e)) return
        setSettled((prev) => ({ key: reloadKey, items: prev?.items ?? null, error: toApiError(e) }))
      },
    )
    return () => controller.abort()
  }, [reloadKey])

  const items = settled?.items ?? null
  const byRepo = useMemo(() => new Map((items ?? []).map((r) => [r.repository.id, r])), [items])
  const reload = useCallback(() => setReloadKey((k) => k + 1), []) // 穩定的函式：可放進 effect 的依賴列
  return {
    items,
    byRepo,
    loading: settled?.key !== reloadKey,
    error: settled?.key === reloadKey ? settled.error : null,
    reload,
  }
}

/** PUT /repositories/{id}/reference：把某個已匯入完成的快照設為該 repo 的參考版本（upsert）。 */
export async function setReference(repoId: number, snapshotId: number): Promise<ReferenceSetResult> {
  const res = await api<ReferenceSetResult>('PUT', `/api/v1/repositories/${repoId}/reference`, {
    body: { snapshot_id: snapshotId },
  })
  return res.data
}

/** DELETE /repositories/{id}/reference：取消參考（冪等，本來就沒有也回 204）。 */
export async function clearReference(repoId: number): Promise<void> {
  await api<void>('DELETE', `/api/v1/repositories/${repoId}/reference`)
}
