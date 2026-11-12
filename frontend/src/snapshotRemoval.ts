// 移除快照（DELETE /snapshots/{id}?force=）的呼叫與結果分類——純邏輯，不含 React，方便單獨驗證。
// 結果分類（RemoveOutcome）讓單一移除與批次移除共用同一套判斷：
//   removed     200：已移除（was_referenced 表示它原本是參考版本，這次 force=true 一併取消了參考）
//   gone        404 snapshot_not_found：已經不在了，對使用者而言就是「已移除」
//   referenced  409 snapshot_referenced：是參考版本，沒有帶 force；需確認後以 force=true 重送
//   busy        409 snapshot_busy：正在匯入或有進行中的工作；稍後再試（force 無效）
//   failed      其他錯誤（422、500、連線失敗…）

import { api, ApiError } from './api'
import { toApiError } from './format'
import type { SnapshotRemoved } from './types'

export type RemoveOutcome =
  | { kind: 'removed'; result: SnapshotRemoved }
  | { kind: 'gone' }
  | { kind: 'referenced'; error: ApiError }
  | { kind: 'busy'; error: ApiError }
  | { kind: 'failed'; error: ApiError }

/** 例外 → 分類；呼叫端不必各自判斷 status／code。 */
export function outcomeFromError(e: unknown): RemoveOutcome {
  const error = toApiError(e)
  if (error.status === 404 && error.code === 'snapshot_not_found') return { kind: 'gone' }
  if (error.status === 409 && error.code === 'snapshot_referenced') return { kind: 'referenced', error }
  if (error.status === 409 && error.code === 'snapshot_busy') return { kind: 'busy', error }
  return { kind: 'failed', error }
}

/** 移除一個快照。force＝它是參考版本時一併取消參考（使用者已確認才帶 true）。 */
export async function removeSnapshot(snapshotId: number, force: boolean): Promise<RemoveOutcome> {
  try {
    const res = await api<SnapshotRemoved>('DELETE', `/api/v1/snapshots/${snapshotId}?force=${force ? 'true' : 'false'}`)
    return { kind: 'removed', result: res.data }
  } catch (e) {
    return outcomeFromError(e)
  }
}

/** 批次中的一筆：id 與目前結果（null＝還沒處理）。 */
export interface BatchEntry {
  id: number
  outcome: RemoveOutcome | null
}

export interface BatchSummary {
  removed: number // 實際移除（含連同參考一起移除的）
  gone: number // 本來就不在了
  referenced: number[] // 因為是參考版本而沒有移除（待使用者決定是否 force）
  busy: number[] // 進行中，略過
  failed: { id: number; error: ApiError }[]
  pending: number // 還沒處理（例如中途離開）
}

/** 彙整批次結果（只看每一筆「最新」的 outcome）。 */
export function summarizeBatch(entries: readonly BatchEntry[]): BatchSummary {
  const summary: BatchSummary = { removed: 0, gone: 0, referenced: [], busy: [], failed: [], pending: 0 }
  for (const entry of entries) {
    const o = entry.outcome
    if (o === null) summary.pending++
    else if (o.kind === 'removed') summary.removed++
    else if (o.kind === 'gone') summary.gone++
    else if (o.kind === 'referenced') summary.referenced.push(entry.id)
    else if (o.kind === 'busy') summary.busy.push(entry.id)
    else summary.failed.push({ id: entry.id, error: o.error })
  }
  return summary
}
