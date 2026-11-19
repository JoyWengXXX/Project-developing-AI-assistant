import { useEffect, useState } from 'react'
import { api, ApiError, isAbortError } from './api'
import type { SnapshotDetail } from './types'

/**
 * 引用指向的快照現在還在不在：
 *   exists＝存在且已完成（可以開啟檔案內容）；missing＝已被移除（後端 404）；unknown＝查不到（網路、非完成狀態等），不提供連結。
 * 問答記錄只保存「當時」的來源位置（快照被移除後記錄照舊可讀），所以連結前要先確認快照還在。
 */
export type SnapshotPresence = 'exists' | 'missing' | 'unknown'

interface Settled {
  key: string
  presence: ReadonlyMap<number, SnapshotPresence>
}

/**
 * 逐一讀取（GET /snapshots/{id}，唯讀）這些快照的狀態；沒有結果前回傳 null（呼叫端先當純文字顯示）。
 * ids 的內容改變時取消舊請求、重新讀取；卸載時一併取消。
 */
export function useSnapshotPresence(ids: readonly number[]): ReadonlyMap<number, SnapshotPresence> | null {
  const unique = [...new Set(ids)].sort((a, b) => a - b)
  const key = unique.join(',')
  const [settled, setSettled] = useState<Settled | null>(null)

  useEffect(() => {
    if (key === '') return
    const controller = new AbortController()
    const wanted = key.split(',').map(Number)
    Promise.all(
      wanted.map(async (id): Promise<[number, SnapshotPresence]> => {
        try {
          const res = await api<SnapshotDetail>('GET', `/api/v1/snapshots/${id}`, { signal: controller.signal })
          return [id, res.data.status === 'completed' ? 'exists' : 'unknown']
        } catch (e) {
          if (isAbortError(e)) throw e
          return [id, e instanceof ApiError && e.status === 404 ? 'missing' : 'unknown']
        }
      }),
    ).then(
      (entries) => setSettled({ key, presence: new Map(entries) }),
      () => {
        /* 被取消：忽略 */
      },
    )
    return () => controller.abort()
  }, [key])

  if (key === '') return EMPTY
  return settled !== null && settled.key === key ? settled.presence : null
}

const EMPTY: ReadonlyMap<number, SnapshotPresence> = new Map()
