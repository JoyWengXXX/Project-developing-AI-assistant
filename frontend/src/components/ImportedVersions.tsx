import { useCallback, useEffect, useRef, useState } from 'react'
import { commitShortSha } from '../format'
import {
  applyRange,
  pruneSelection,
  quickSelectCount,
  quickSelectIds,
  selectionState,
  selectionTotals,
  toggleAll,
  toggleId,
  type QuickSelect,
  type RefBadge,
} from '../importedList'
import { formatSize } from '../jobFormat'
import { removeSnapshot } from '../snapshotRemoval'
import type { SnapshotItem } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { useBatchRemoval } from '../useBatchRemoval'
import type { SnapshotListState } from '../useSnapshotList'
import { BatchResult } from './BatchResult'
import { ConfirmBox } from './ConfirmBox'
import { ImportedVersionRow } from './ImportedVersionRow'
import { SearchPanel } from './SearchPanel'
import { SnapshotDetailView } from './SnapshotDetailView'
import { StructurePanel } from './StructurePanel'

interface Props {
  /** 這個 repo 的所有快照（含失敗、過期設定；由版本區讀取，與「已匯入」的判斷共用同一份資料）。 */
  list: SnapshotListState
  /** 快照有變動時遞增（詳情據此重新讀取）。 */
  refreshKey: number
  /** 上方目前選取的 commit（清單中對應的那一列會標示）。 */
  selectedSha: string | null
  refsBySha: ReadonlyMap<string, readonly RefBadge[]>
  subjectBySha: ReadonlyMap<string, string>
  /** 從總覽的「管理已匯入版本」進來：載入後把焦點移到這個區塊的標題。 */
  focusOnMount: boolean
  /** 點清單的一列：選取該 commit（必須是穩定的函式）。 */
  onPick: (sha: string) => void
  /** 移除了快照（或發現早已不存在）：父元件重新讀取清單與 commits 的 imported 標記。 */
  onChanged: () => void
  onReload: () => void
}

// 目前開著的確認對話：單一移除，或批次移除（批次中遇到參考版本後的「連同取消參考」由 batch.forceIds 控制）
type Confirm = { kind: 'single'; snapshot: SnapshotItem } | { kind: 'batch' }

/**
 * 「已匯入的版本」：此 repo 的所有快照，一覽、勾選、批次移除。放在版本區的 commit 圖旁邊，預設展開，
 * 清單有最大高度與內部捲動（表頭固定）；有勾選時，底部的操作列固定在視窗底部，不必捲動就能按「移除選取的版本」。
 *
 * 移除只刪資料庫裡的快照紀錄，不動 Git repo。批次移除的規則在 useBatchRemoval（逐一 DELETE；
 * 參考版本不自動 force、彙整後才詢問；busy 略過；404 視為已移除）。
 * 清單一次載入全部（useSnapshotList），選取與總計都在前端計算。
 */
export function ImportedVersions({
  list,
  refreshKey,
  selectedSha,
  refsBySha,
  subjectBySha,
  focusOnMount,
  onPick,
  onChanged,
  onReload,
}: Props) {
  const items = list.items
  const rows = items ?? []
  const [selected, setSelected] = useState<ReadonlySet<number>>(new Set())
  const [confirm, setConfirm] = useState<Confirm | null>(null)
  const [labels, setLabels] = useState<ReadonlyMap<number, string>>(new Map()) // 批次結果用：編號 → 短 SHA
  const [detailOpen, setDetailOpen] = useState(false)
  const [structureOpen, setStructureOpen] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const singleAction = useAction()
  const { clear: clearSingleNotice } = singleAction
  const headingRef = useRef<HTMLHeadingElement>(null)
  const anchor = useRef<number | null>(null) // Shift＋點擊範圍選取的起點
  const rowsRef = useRef<SnapshotItem[]>([])
  useEffect(() => {
    rowsRef.current = rows
  })
  useEffect(() => {
    if (focusOnMount) headingRef.current?.focus()
  }, [focusOnMount])

  /** 移除成功（或早已不存在）之後：整理選取，通知父元件重新讀取。 */
  const afterRemoved = useCallback(
    (ids: number[]) => {
      setSelected((prev) => new Set([...prev].filter((id) => !ids.includes(id))))
      onChanged()
    },
    [onChanged],
  )
  const batch = useBatchRemoval(afterRemoved)
  const busy = singleAction.busy || batch.running

  // 列表重新讀取後，已不存在的項目不再算被選取（例如在別處被移除）
  const [prevItems, setPrevItems] = useState(items)
  if (items !== prevItems) {
    setPrevItems(items)
    if (items !== null) setSelected((prev) => pruneSelection(items, prev))
  }

  const toggle = useCallback((id: number, shiftKey: boolean) => {
    const from = shiftKey ? anchor.current : null
    anchor.current = id
    setSelected((prev) => {
      const checked = !prev.has(id)
      return from !== null ? applyRange(rowsRef.current, prev, from, id, checked) : toggleId(prev, id)
    })
  }, [])

  const askRemove = useCallback(
    (snapshot: SnapshotItem) => {
      clearSingleNotice()
      setConfirm({ kind: 'single', snapshot })
    },
    [clearSingleNotice],
  )

  const state = selectionState(rows, selected)
  const picked = selectionTotals(rows, selected)
  const totalBytes = rows.reduce((sum, s) => sum + s.total_bytes, 0)
  const referencedCount = rows.filter((s) => s.is_referenced).length
  const currentRowSnapshot = selectedSha !== null ? (rows.find((s) => s.commit_sha === selectedSha) ?? null) : null

  function quickSelect(rule: QuickSelect) {
    anchor.current = null
    setSelected(quickSelectIds(rows, rule))
  }

  async function removeOne(snapshot: SnapshotItem) {
    const short = commitShortSha(snapshot.commit_sha)
    const ok = await singleAction.run(async () => {
      const outcome = await removeSnapshot(snapshot.id, snapshot.is_referenced) // 參考版本：確認對話已說明，帶 force=true
      if (outcome.kind === 'removed') {
        afterRemoved([snapshot.id])
        const r = outcome.result
        return `已移除版本 ${short}（刪除 ${r.deleted_files.toLocaleString()} 個檔案${r.was_referenced ? '，並一併取消參考' : ''}）。`
      }
      if (outcome.kind === 'gone') {
        afterRemoved([snapshot.id])
        return `版本 ${short} 已不存在，清單已重新整理。`
      }
      onChanged() // referenced（剛被設為參考）、busy 等：資料可能已過期，重新讀取
      throw outcome.error
    })
    setConfirm(null)
    // 被移除的那一列（與按鈕）已不在畫面上：把焦點移到標題，鍵盤使用者可以接著往下 Tab
    if (ok) requestAnimationFrame(() => document.activeElement === document.body && headingRef.current?.focus())
  }

  function startBatch() {
    setConfirm(null)
    clearSingleNotice()
    setLabels(new Map(picked.items.map((s) => [s.id, commitShortSha(s.commit_sha)])))
    batch.start(picked.items.map((s) => s.id))
  }

  const labelOf = (id: number) => labels.get(id) ?? `#${id}`
  const showForce = batch.forceIds !== null && confirm === null
  const showBar = picked.items.length > 0 && confirm === null && !showForce && !batch.running
  const dockVisible = confirm !== null || showForce || showBar || batch.running

  return (
    <section className="imported-versions" aria-labelledby="imported-title">
      <div className="imported-title-row">
        <h3 id="imported-title" tabIndex={-1} ref={headingRef}>
          已匯入的版本{items !== null && `（${items.length}）`}
        </h3>
        <button type="button" className="secondary small" disabled={busy || list.loading} onClick={onReload}>
          {list.loading ? '載入中…' : '重新整理'}
        </button>
      </div>
      <p className="note">
        存進資料庫的版本。勾選後可一次移除；只會刪除資料庫裡的紀錄，不會動你的 Git repo，之後可以再匯入。
      </p>

      {list.loading && items === null && <p role="status">載入已匯入的版本…</p>}
      <ErrorBox error={list.error} />
      {list.truncated && (
        <p className="warning" role="alert">
          版本數量很多，只載入了最新的一部分；請先移除不需要的版本。
        </p>
      )}
      {items !== null && items.length === 0 && (
        <p className="empty">還沒有匯入任何版本。在右側的 commit 圖選取 commit，按「匯入此版本」就會出現在這裡。</p>
      )}

      {items !== null && items.length > 0 && (
        <>
          <p className="note" role="status">
            共 {items.length.toLocaleString()} 個版本，大小合計 {formatSize(totalBytes)}
            {referencedCount > 0 && `（其中 ${referencedCount} 個是參考版本）`}
          </p>
          <div className="action-row quick-select" role="group" aria-label="快速選取">
            <button type="button" className="secondary small" disabled={busy || quickSelectCount(rows, 'failed') === 0} onClick={() => quickSelect('failed')}>
              選取失敗的（{quickSelectCount(rows, 'failed')}）
            </button>
            <button type="button" className="secondary small" disabled={busy || quickSelectCount(rows, 'stale') === 0} onClick={() => quickSelect('stale')}>
              選取過期設定的（{quickSelectCount(rows, 'stale')}）
            </button>
            <button type="button" className="secondary small" disabled={busy || quickSelectCount(rows, 'unreferenced') === 0} onClick={() => quickSelect('unreferenced')}>
              選取未被參考的（{quickSelectCount(rows, 'unreferenced')}）
            </button>
            <button type="button" className="secondary small" disabled={busy || picked.items.length === 0} onClick={() => setSelected(new Set())}>
              清除選取
            </button>
          </div>

          <div className="imported-scroll">
            <div className="imported-head">
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={state === 'all'}
                  disabled={busy}
                  ref={(el) => {
                    if (el) el.indeterminate = state === 'some' // 部分選取：顯示「－」；螢幕閱讀器念成「部分勾選」
                  }}
                  onChange={() => setSelected(toggleAll(rows, selected))}
                />
                全選
              </label>
              <span className="muted">
                {picked.items.length > 0 ? `已選 ${picked.items.length} 個` : 'Shift＋點擊核取方塊可選取一段範圍'}
              </span>
            </div>
            <ul className="imported-list" aria-label="已匯入的版本">
              {rows.map((s) => (
                <ImportedVersionRow
                  key={s.id}
                  snapshot={s}
                  checked={selected.has(s.id)}
                  current={s.commit_sha === selectedSha}
                  refs={refsBySha.get(s.commit_sha) ?? NO_REFS}
                  subject={subjectBySha.get(s.commit_sha) ?? null}
                  disabled={busy}
                  onToggle={toggle}
                  onPick={onPick}
                  onRemove={askRemove}
                />
              ))}
            </ul>
          </div>

          {currentRowSnapshot !== null && (
            <details className="snapshot-detail-toggle" open={detailOpen} onToggle={(e) => setDetailOpen(e.currentTarget.open)}>
              <summary>選取版本的快照詳情（進階）</summary>
              {detailOpen && <SnapshotDetailView key={currentRowSnapshot.id} snapshotId={currentRowSnapshot.id} refreshKey={refreshKey} />}
            </details>
          )}
          {currentRowSnapshot !== null && currentRowSnapshot.status === 'completed' && (
            <details className="snapshot-detail-toggle" open={structureOpen} onToggle={(e) => setStructureOpen(e.currentTarget.open)}>
              <summary>程式結構（Python 符號、部分解析的檔案）</summary>
              {structureOpen && <StructurePanel key={currentRowSnapshot.id} snapshotId={currentRowSnapshot.id} />}
            </details>
          )}
          {currentRowSnapshot !== null && currentRowSnapshot.status === 'completed' && (
            <details className="snapshot-detail-toggle" open={searchOpen} onToggle={(e) => setSearchOpen(e.currentTarget.open)}>
              <summary>搜尋（檔名、符號、內容）</summary>
              {searchOpen && <SearchPanel key={currentRowSnapshot.id} snapshotId={currentRowSnapshot.id} />}
            </details>
          )}
        </>
      )}

      {singleAction.notice !== null && (
        <p className="success" role="status">
          {singleAction.notice}
        </p>
      )}
      <ErrorBox error={singleAction.error} />
      {/* 批次結果：進行中顯示在底部操作區（固定在視窗底部），結束後留在清單下方直到關閉 */}
      {batch.view !== null && !batch.view.running && <BatchResult view={batch.view} labelOf={labelOf} onDismiss={batch.dismiss} />}

      {/* 固定在視窗底部的操作區：進度、確認對話，或「已選 N 個」與移除按鈕 */}
      {dockVisible && (
        <div className="imported-dock">
          {batch.view !== null && batch.view.running && <BatchResult view={batch.view} labelOf={labelOf} onDismiss={batch.dismiss} />}
          {confirm?.kind === 'single' && (
            <ConfirmBox
              title={`確定要移除版本 ${commitShortSha(confirm.snapshot.commit_sha)} 嗎？`}
              confirmLabel={confirm.snapshot.is_referenced ? '移除並取消參考' : '確定移除'}
              busy={singleAction.busy}
              onConfirm={() => void removeOne(confirm.snapshot)}
              onCancel={() => setConfirm(null)}
            >
              <p>只會刪除資料庫中這個版本的紀錄，不會動你的 Git repo；之後可以再按「匯入此版本」重新匯入。</p>
              {confirm.snapshot.is_referenced && <p>這個版本是目前的參考版本，移除會同時取消參考。</p>}
            </ConfirmBox>
          )}
          {confirm?.kind === 'batch' && (
            <ConfirmBox
              title={`確定要移除選取的 ${picked.items.length} 個版本嗎？（共 ${formatSize(picked.bytes)}）`}
              confirmLabel="確定移除"
              onConfirm={startBatch}
              onCancel={() => setConfirm(null)}
            >
              <p>只會刪除資料庫中這些版本的紀錄，不會動你的 Git repo；會一個一個依序移除。</p>
              <p>
                共 {picked.items.length} 個版本，大小合計 {formatSize(picked.bytes)}；其中參考版本 {picked.referenced} 個
                {picked.referenced > 0 && '：這一步不會移除它們，全部處理完後會再問你是否連同取消參考'}。
              </p>
              {picked.importing > 0 && <p>匯入中的版本若仍有進行中的工作，會被略過。</p>}
            </ConfirmBox>
          )}
          {showForce && batch.forceIds !== null && (
            <ConfirmBox
              title={`有 ${batch.forceIds.length} 個版本是參考版本，尚未移除`}
              confirmLabel="連同取消參考一併移除"
              onConfirm={batch.confirmForce}
              onCancel={batch.declineForce}
            >
              <p>
                要連同取消參考一起移除嗎？（{batch.forceIds.map(labelOf).join('、')}）移除後這些 repo 就沒有參考版本，可以之後再重新設定。
              </p>
            </ConfirmBox>
          )}
          {showBar && (
            <div className="imported-bar">
              <span role="status">
                已選 <strong>{picked.items.length}</strong> 個（共 {formatSize(picked.bytes)}）
                {picked.referenced > 0 && `，含參考版本 ${picked.referenced} 個`}
              </span>
              <button type="button" className="danger-solid" disabled={busy} onClick={() => setConfirm({ kind: 'batch' })}>
                移除選取的版本
              </button>
              <button type="button" className="secondary" disabled={busy} onClick={() => setSelected(new Set())}>
                清除選取
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  )
}

const NO_REFS: readonly RefBadge[] = []
