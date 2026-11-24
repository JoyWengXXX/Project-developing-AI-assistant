import type { ApiError } from '../api'
import { qaErrorText, type QaErrorInput } from '../qaErrors'

/**
 * 問答的錯誤：先用繁體中文說明（依錯誤碼；額度、同意的 repo 等細節取自錯誤的 details），
 * 再列出後端的錯誤碼、HTTP 狀態與 request_id，方便對照後端日誌。
 * 提問的 HTTP 錯誤（ApiError）與問答失敗後記錄的 error（{ code, message }）都用它顯示。
 */
export function QaErrorBox({ error, title = '錯誤', requestId = null }: {
  error: ApiError | QaErrorInput | null
  title?: string
  requestId?: string | null
}) {
  if (error === null) return null
  const text = qaErrorText(error)
  const status = 'status' in error ? error.status : null
  const rid = 'requestId' in error ? error.requestId : requestId
  return (
    <div className="error-box qa-error" role="alert">
      <div className="error-title">{title}</div>
      <p className="qa-error-summary">{text.summary}</p>
      {text.advice !== null && <p className="error-hint">{text.advice}</p>}
      <details className="error-tech">
        <summary>技術細節{status !== null && status !== undefined && <>（HTTP {status}）</>}</summary>
        <p className="qa-error-code">
          code <code>{error.code}</code>
          {rid !== null && rid !== undefined && (
            <>
              ・request_id <code>{rid}</code>
            </>
          )}
        </p>
      </details>
    </div>
  )
}
