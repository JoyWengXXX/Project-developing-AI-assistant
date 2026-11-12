import { useEffect, useRef, useState } from 'react'
import { removeSnapshot, summarizeBatch, type BatchEntry } from './snapshotRemoval'

/** 批次移除的進度與逐筆結果（序列化：一次只送一個 DELETE）。 */
export interface BatchView {
  entries: BatchEntry[]
  running: boolean
  /** 這一輪要處理幾筆、已處理幾筆（顯示進度用）。 */
  total: number
  done: number
}

export interface BatchRemoval {
  view: BatchView | null
  /** 批次中遇到參考版本（沒有移除）的快照編號：等使用者決定是否「連同取消參考」；沒有要問時為 null。 */
  forceIds: number[] | null
  running: boolean
  start: (ids: number[]) => void
  /** 使用者同意：只重送參考版本（force=true）。 */
  confirmForce: () => void
  /** 使用者不同意：參考版本保持原狀。 */
  declineForce: () => void
  dismiss: () => void
}

/**
 * 批次移除快照（沿用 snapshotRemoval 的分類）：逐一 DELETE，一次一個；
 * 遇到參考版本（snapshot_referenced）不會自動 force，全部跑完才彙整詢問；snapshot_busy 略過；404 視為已移除。
 * onRemoved：每一輪結束後，把「已移除（含早已不存在）」的快照編號交給呼叫端（重新讀取、整理選取）。
 * 元件卸載（離開詳情、換 repo）後不再送出剩下的 DELETE。
 */
export function useBatchRemoval(onRemoved: (ids: number[]) => void): BatchRemoval {
  const [view, setView] = useState<BatchView | null>(null)
  const [forceIds, setForceIds] = useState<number[] | null>(null)
  const running = useRef(false)
  const lastEntries = useRef<BatchEntry[]>([])
  const alive = useRef(true)
  const onRemovedRef = useRef(onRemoved)
  useEffect(() => {
    onRemovedRef.current = onRemoved
  })
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  async function process(entries: BatchEntry[], targetIds: number[], force: boolean) {
    if (running.current) return
    running.current = true
    const current = entries.map((e) => ({ ...e }))
    const snapshot = (isRunning: boolean, done: number): BatchView => ({
      entries: current.map((e) => ({ ...e })),
      running: isRunning,
      total: targetIds.length,
      done,
    })
    setForceIds(null)
    setView(snapshot(true, 0))
    let done = 0
    for (const id of targetIds) {
      if (!alive.current) break
      const outcome = await removeSnapshot(id, force)
      const entry = current.find((e) => e.id === id)
      if (entry !== undefined) entry.outcome = outcome
      done++
      if (alive.current) setView(snapshot(true, done))
    }
    running.current = false
    lastEntries.current = current
    if (alive.current) setView(snapshot(false, done))
    onRemovedRef.current(current.filter((e) => e.outcome?.kind === 'removed' || e.outcome?.kind === 'gone').map((e) => e.id))
    // 有參考版本被略過：彙整後詢問是否連同取消參考（不自動 force）
    const referenced = summarizeBatch(current).referenced
    if (alive.current && !force && referenced.length > 0) setForceIds(referenced)
  }

  return {
    view,
    forceIds,
    running: view?.running === true,
    start: (ids) => void process(ids.map((id) => ({ id, outcome: null })), ids, false),
    // 沿用上一輪的逐筆結果，只重送參考版本
    confirmForce: () => {
      if (forceIds !== null) void process(lastEntries.current, forceIds, true)
    },
    declineForce: () => setForceIds(null),
    dismiss: () => setView(null),
  }
}
