import { summarizeBatch } from '../snapshotRemoval'
import type { BatchView } from '../useBatchRemoval'

interface Props {
  view: BatchView
  /** 編號 → 顯示名稱（短 SHA）；找不到時顯示 #編號。 */
  labelOf: (snapshotId: number) => string
  onDismiss: () => void
}

/**
 * 批次移除的進度與結果：進行中是進度條（aria-live，螢幕閱讀器會朗讀「N / 總數」），
 * 結束後是一行彙整（已移除、早已不存在、參考版本未移除、略過、失敗）與可捲動的逐筆結果。
 */
export function BatchResult({ view, labelOf, onDismiss }: Props) {
  const summary = summarizeBatch(view.entries)
  return (
    <div className="batch-result">
      {view.running ? (
        <div role="status" aria-live="polite" aria-busy="true">
          <p className="note">
            批次移除中：{view.done} / {view.total}（一次移除一個）…
          </p>
          <progress value={view.done} max={view.total} aria-label="批次移除進度" />
        </div>
      ) : (
        <div className="action-row">
          <p className="note" role="status" aria-live="polite">
            批次移除完成：已移除 {summary.removed} 個
            {summary.gone > 0 && `、早已不存在 ${summary.gone} 個`}
            {summary.referenced.length > 0 && `、參考版本未移除 ${summary.referenced.length} 個`}
            {summary.busy.length > 0 && `、進行中略過 ${summary.busy.length} 個`}
            {summary.failed.length > 0 && `、失敗 ${summary.failed.length} 個`}
            {summary.pending > 0 && `、未處理 ${summary.pending} 個`}。
          </p>
          <button type="button" className="secondary small" onClick={onDismiss}>
            關閉結果
          </button>
        </div>
      )}
      <ul className="batch-list" aria-label="批次移除逐筆結果" hidden={view.running}>
        {view.entries.map((e) => {
          const o = e.outcome
          return (
            <li key={e.id}>
              <code>{labelOf(e.id)}</code>{' '}
              {o === null && <span className="muted">等待中</span>}
              {o?.kind === 'removed' && (
                <span>
                  已移除（刪除 {o.result.deleted_files.toLocaleString()} 個檔案{o.result.was_referenced ? '，並取消參考' : ''}）
                </span>
              )}
              {o?.kind === 'gone' && <span>早已不存在（視為已移除）</span>}
              {o?.kind === 'referenced' && <span>參考版本，未移除（需確認後才會連同取消參考）</span>}
              {o?.kind === 'busy' && <span>略過：這個版本正在匯入或有進行中的工作，請稍後再試</span>}
              {o?.kind === 'failed' && (
                <span>
                  失敗：<code>{o.error.code}</code> {o.error.message}
                </span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
