// 多個面板共用的小元件。

import { type ApiError } from './api'
import { ERROR_HINT, SNAPSHOT_STATUS_LABEL } from './format'

/** 「這個操作對應哪支 API」的提示。 */
export function ApiHint({ apis }: { apis: string[] }) {
  return (
    <p className="api-hint">
      對應 API：
      {apis.map((a) => (
        <code key={a}>{a}</code>
      ))}
    </p>
  )
}

/**
 * 統一錯誤格式的顯示：先給白話說明（沒有對應說明時用後端 message），
 * HTTP 狀態碼、code、message、request_id 與 422 的 details 收在「技術細節」。
 * hints：特定情境的說明表（例如移除 repo），優先於通用的 ERROR_HINT。
 */
export function ErrorBox({ error, hints }: { error: ApiError | null; hints?: Record<string, string> }) {
  if (error === null) return null
  const hint = hints?.[error.code] ?? ERROR_HINT[error.code]
  return (
    <div className="error-box" role="alert">
      <div className="error-title">沒有完成</div>
      <p className="error-hint">{hint ?? error.message}</p>
      <details className="error-tech">
        <summary>技術細節{error.status !== null && <>（HTTP {error.status}）</>}</summary>
      <dl className="kv">
        <dt>code</dt>
        <dd>
          <code>{error.code}</code>
        </dd>
        <dt>message</dt>
        <dd>{error.message}</dd>
        <dt>request_id</dt>
        <dd>{error.requestId ? <code>{error.requestId}</code> : '（無）'}</dd>
      </dl>
      {Array.isArray(error.details) && error.details.length > 0 && (
        <div>
          <div className="error-title">details（哪個欄位不合法）</div>
          <ul>
            {error.details.map((d, i) => (
              <li key={i}>
                <code>{d.loc.join('.')}</code>：{d.msg}（{d.type}）
              </li>
            ))}
          </ul>
        </div>
      )}
      {error.details && !Array.isArray(error.details) && (
        <p>
          {error.details.snapshot_id !== undefined && <>快照 #{error.details.snapshot_id} </>}
          {error.details.index_status !== undefined && <>索引狀態：{error.details.index_status}</>}
        </p>
      )}
      </details>
    </div>
  )
}

export function SnapshotStatusBadge({ status }: { status: string }) {
  return <span className={`badge badge-${status}`}>{SNAPSHOT_STATUS_LABEL[status] ?? status}</span>
}

interface PagerProps {
  total: number
  limit: number
  offset: number
  onChange: (offset: number) => void
}

/** 分頁：上一頁／下一頁；offset 由父元件保存。 */
export function Pager({ total, limit, offset, onChange }: PagerProps) {
  const from = total === 0 ? 0 : offset + 1
  const to = Math.min(offset + limit, total)
  return (
    <div className="pager">
      <span>
        第 {from}–{to} 筆，共 {total} 筆
      </span>
      <button type="button" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>
        上一頁
      </button>
      <button type="button" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>
        下一頁
      </button>
    </div>
  )
}
