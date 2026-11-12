// 「已匯入的版本」清單的純邏輯（不含 React，方便單獨驗證）：
// 選取集合、全選／部分選取（indeterminate）、Shift 範圍選取、快速選取規則、選取的總計，以及
// 「commit SHA → 分支／標籤徽章」的對照。注意：只匯入型別，讓暫時的 Node 腳本可以直接載入。

import type { RefsResponse, SnapshotItem } from './types'

/** 清單的選取狀態：none＝沒有選、some＝部分（核取方塊顯示 indeterminate）、all＝全選。 */
export type SelectionState = 'none' | 'some' | 'all'

export function selectionState(items: readonly SnapshotItem[], selected: ReadonlySet<number>): SelectionState {
  if (items.length === 0) return 'none'
  const count = items.filter((s) => selected.has(s.id)).length
  if (count === 0) return 'none'
  return count === items.length ? 'all' : 'some'
}

/** 單一項目切換（回傳新集合，不改動傳入的集合）。 */
export function toggleId(selected: ReadonlySet<number>, id: number): Set<number> {
  const next = new Set(selected)
  if (!next.delete(id)) next.add(id)
  return next
}

/** 表頭的全選：目前已全選就清除，否則（沒選或部分選）全部選取。 */
export function toggleAll(items: readonly SnapshotItem[], selected: ReadonlySet<number>): Set<number> {
  return selectionState(items, selected) === 'all' ? new Set() : new Set(items.map((s) => s.id))
}

/**
 * Shift＋點擊的範圍選取：把 anchorId 到 targetId（含兩端，依列表順序）全部設成 checked
 * （checked 是被點的那個核取方塊「點完之後」的狀態）。找不到起點時只處理被點的那一個。
 */
export function applyRange(
  items: readonly SnapshotItem[],
  selected: ReadonlySet<number>,
  anchorId: number | null,
  targetId: number,
  checked: boolean,
): Set<number> {
  const to = items.findIndex((s) => s.id === targetId)
  const from = anchorId === null ? -1 : items.findIndex((s) => s.id === anchorId)
  const next = new Set(selected)
  if (to < 0) return next
  const [start, end] = from < 0 ? [to, to] : [Math.min(from, to), Math.max(from, to)]
  for (const s of items.slice(start, end + 1)) {
    if (checked) next.add(s.id)
    else next.delete(s.id)
  }
  return next
}

/** 快速選取的規則（會取代目前的選取）。 */
export type QuickSelect = 'failed' | 'stale' | 'unreferenced'

const QUICK_RULES: Record<QuickSelect, (s: SnapshotItem) => boolean> = {
  failed: (s) => s.status === 'failed',
  stale: (s) => s.is_stale_profile,
  unreferenced: (s) => !s.is_referenced,
}

export function quickSelectIds(items: readonly SnapshotItem[], rule: QuickSelect): Set<number> {
  return new Set(items.filter(QUICK_RULES[rule]).map((s) => s.id))
}

export function quickSelectCount(items: readonly SnapshotItem[], rule: QuickSelect): number {
  return items.filter(QUICK_RULES[rule]).length
}

/** 只保留還在列表裡的選取（列表重新讀取、項目被移除後呼叫）。 */
export function pruneSelection(items: readonly SnapshotItem[], selected: ReadonlySet<number>): Set<number> {
  const alive = new Set(items.map((s) => s.id))
  return new Set([...selected].filter((id) => alive.has(id)))
}

export interface SelectionTotals {
  items: SnapshotItem[] // 被選取的項目（列表順序）
  bytes: number
  referenced: number // 其中被參考的數量
  importing: number // 其中匯入中的數量（可能因進行中的工作而被略過）
}

export function selectionTotals(items: readonly SnapshotItem[], selected: ReadonlySet<number>): SelectionTotals {
  const picked = items.filter((s) => selected.has(s.id))
  return {
    items: picked,
    bytes: picked.reduce((sum, s) => sum + s.total_bytes, 0),
    referenced: picked.filter((s) => s.is_referenced).length,
    importing: picked.filter((s) => s.status === 'importing').length,
  }
}

/** 清單上顯示的分支／標籤徽章。 */
export interface RefBadge {
  name: string
  kind: 'branch' | 'tag'
  isDefault: boolean
}

/** 由 refs 回應建立「commit SHA → 指向它的分支／標籤」（只有分支或標籤的最新 commit 對得上，其餘不顯示）。 */
export function refsBySha(refs: RefsResponse | null): Map<string, RefBadge[]> {
  const map = new Map<string, RefBadge[]>()
  if (refs === null) return map
  const add = (sha: string, badge: RefBadge) => map.set(sha, [...(map.get(sha) ?? []), badge])
  for (const b of refs.branches) add(b.sha, { name: b.name, kind: 'branch', isDefault: b.is_default })
  for (const t of refs.tags) add(t.sha, { name: t.name, kind: 'tag', isDefault: false })
  return map
}
