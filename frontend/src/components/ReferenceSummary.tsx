import type { ApiError } from '../api'
import { commitShortSha, referenceTitle } from '../format'
import type { ReferenceItem } from '../types'
import { ErrorBox } from '../ui'

interface Props {
  items: ReferenceItem[] | null // GET /references；第一次載入前為 null
  loading: boolean
  error: ApiError | null
  /** 取消參考的動作進行中（一次只做一個）。 */
  busy: boolean
  /** 取消參考（DELETE reference）後的錯誤。 */
  actionError: ApiError | null
  onClear: (item: ReferenceItem) => void
}

/** 參考清單摘要（讀後端資料）：每個 repo 的參考版本（repo 名稱＋commit 短 SHA）與「取消」，並明確標示這目前只是標記。 */
export function ReferenceSummary({ items, loading, error, busy, actionError, onClear }: Props) {
  const list = items ?? []
  return (
    <section className="reference-summary" aria-labelledby="reference-title">
      <h3 id="reference-title">參考版本 {items === null ? '' : `（${list.length} 個）`}</h3>
      {items === null && loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      <ErrorBox error={actionError} />
      {items !== null && list.length === 0 && (
        <p className="note">還沒有設定任何參考版本。在 repo 卡片的「參考版本」區，從已匯入的版本中選一個設為參考。</p>
      )}
      {list.length > 0 && (
        <ul className="reference-list">
          {list.map((r) => (
            <li key={r.id} className="reference-chip">
              <span>
                {referenceTitle(r)} <code title={r.commit_sha}>{commitShortSha(r.commit_sha)}</code>
              </span>
              <button
                type="button"
                className="secondary small"
                disabled={busy}
                aria-label={`取消 ${referenceTitle(r)} 的參考版本`}
                onClick={() => onClear(r)}
              >
                取消
              </button>
            </li>
          ))}
        </ul>
      )}
      <p className="note">
        <strong>目前只是標記，尚未連到 Agent。</strong>
        參考版本已存進資料庫（重新整理頁面仍在）；各 repo 的角色（參考／目標）與怎麼被使用，由第五階段的分析任務決定。
      </p>
    </section>
  )
}
