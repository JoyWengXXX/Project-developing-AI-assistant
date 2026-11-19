import { useEffect, useRef, useState } from 'react'
import { formatTime, repoTitle } from '../format'
import type { Repository } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import type { QaConsents } from '../useQaConsents'

/**
 * 某個 repo 的「問答資料外送同意」：預設未同意；同意後顯示時間，可隨時撤回。
 * 用在 repo 卡片（必要位置）與設定面板的清單。同意狀態由 App 的 useQaConsents 持有（一次請求讀全部）。
 * 這裡只是標記：不送出任何程式內容；實際的問答流程不在這一版。
 * 「同意」要先確認一次（它代表程式內容會離開本機）；「撤回」立即生效。
 */
export function QaConsentToggle({ repo, consents }: { repo: Repository; consents: QaConsents }) {
  const action = useAction()
  const [confirming, setConfirming] = useState(false)
  const grantRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const wasConfirming = useRef(false)
  const title = repoTitle(repo)

  // 鍵盤焦點：打開確認區塊時移到「取消」（預設走安全的選項）；取消後回到「同意」按鈕
  useEffect(() => {
    if (confirming) cancelRef.current?.focus()
    else if (wasConfirming.current) grantRef.current?.focus()
    wasConfirming.current = confirming
  }, [confirming])

  const known = consents.byRepo !== null
  const consentedAt = consents.byRepo?.get(repo.id) ?? null
  const consented = consentedAt !== null

  async function grant() {
    const ok = await action.run(async () => {
      await consents.grant(repo.id)
    })
    if (ok) setConfirming(false)
  }

  return (
    <div className="qa-consent" role="group" aria-label={`${title} 的問答資料外送同意`}>
      <div className="qa-consent-head">
        <strong>問答資料外送</strong>
        {!known && consents.loading && <span className="muted">載入同意狀態中…</span>}
        {!known && !consents.loading && <span className="badge badge-failed">無法取得同意狀態</span>}
        {known && !consented && <span className="badge badge-neutral">未同意</span>}
        {known && consented && <span className="badge badge-completed">已同意</span>}
      </div>
      {consented && <p className="note">同意時間：{formatTime(consentedAt)}</p>}
      <p className="note">
        同意後，對這個 repo 提問時，會把檢索到的程式片段送往 Anthropic API（第三方服務）。預設不同意；可隨時撤回，撤回後之後的提問不會再送出此 repo 的片段。
      </p>
      {!known && !consents.loading && (
        <button type="button" className="secondary" onClick={consents.reload}>
          重新載入同意狀態
        </button>
      )}
      {known && !consented && !confirming && (
        <button type="button" ref={grantRef} aria-expanded={confirming} disabled={action.busy} onClick={() => setConfirming(true)}>
          同意把 {title} 的程式片段送往 Anthropic API
        </button>
      )}
      {known && !consented && confirming && (
        <div className="job-confirm" role="group" aria-label="確認資料外送同意" onKeyDown={(e) => {
          if (e.key === 'Escape' && !action.busy) setConfirming(false)
        }}>
          <p>
            <strong>確定同意嗎？</strong>
            這會允許把 {title} 的程式片段送往 Anthropic API（第三方服務）。
          </p>
          <div className="action-row">
            <button type="button" disabled={action.busy} onClick={() => void grant()}>
              {action.busy ? '儲存中…' : '確定同意'}
            </button>
            <button type="button" className="secondary" ref={cancelRef} disabled={action.busy} onClick={() => setConfirming(false)}>
              取消
            </button>
          </div>
        </div>
      )}
      {known && consented && (
        <button
          type="button"
          className="danger"
          disabled={action.busy}
          onClick={() => void action.run(() => consents.revoke(repo.id))}
        >
          {action.busy ? '儲存中…' : `撤回 ${title} 的資料外送同意`}
        </button>
      )}
      <ErrorBox error={action.error} />
    </div>
  )
}
