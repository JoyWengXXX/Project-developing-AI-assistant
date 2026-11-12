import { useState, type FormEvent, type ReactNode } from 'react'
import { api, type ApiError } from '../api'
import { toApiError } from '../format'
import type { GitHubAuth, GitHubTokenType } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useLoad } from '../useLoad'

const TOKEN_SETTINGS_URL = 'https://github.com/settings/personal-access-tokens'

const TOKEN_TYPE_LABEL: Record<GitHubTokenType, string> = {
  fine_grained: 'fine-grained（建議）',
  classic: 'classic',
  unknown: '未知類型',
}

/**
 * GitHub 面板：token 狀態、貼上並驗證 token、刪除本機保存的 token。
 *
 * token 的處理原則：
 * - 只存在輸入框的 state（tokenInput）。按下送出的瞬間就清空輸入框，
 *   請求用的字串只活在 save() 這個函式的區域變數，函式結束後即可被回收。
 * - 不寫入 localStorage／sessionStorage、全域變數、console、URL，也不放進錯誤訊息
 *   （後端的錯誤回應本來就不含 token；ApiError 也沒有保存請求內容）。
 */
export function GitHubPanel() {
  const [reloadKey, setReloadKey] = useState(0)
  const auth = useLoad<GitHubAuth>('/api/v1/github/auth', reloadKey)

  const [tokenInput, setTokenInput] = useState('')
  const [busy, setBusy] = useState<'save' | 'delete' | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function saveToken(e: FormEvent) {
    e.preventDefault()
    if (busy !== null) return
    const token = tokenInput
    setTokenInput('') // 不論成功或失敗，送出後立即清空輸入框
    setBusy('save')
    setError(null)
    setMessage(null)
    try {
      await api<GitHubAuth>('PUT', '/api/v1/github/token', { body: { token } })
      setMessage('token 已通過 GitHub 驗證並加密保存。')
      setReloadKey((k) => k + 1) // 以 GET /github/auth 的結果更新狀態
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setBusy(null)
    }
  }

  async function deleteToken() {
    if (busy !== null) return
    if (!window.confirm('確定要刪除本機保存的 GitHub token 嗎？\n\n這不會在 GitHub 端撤銷 token。')) return
    setBusy('delete')
    setError(null)
    setMessage(null)
    try {
      await api<void>('DELETE', '/api/v1/github/auth')
      setMessage('已刪除本機保存的 token。')
      setReloadKey((k) => k + 1)
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setBusy(null)
    }
  }

  const status = auth.data
  const canDelete = status !== null && (status.authenticated || status.warning !== null)

  // 摺疊區的摘要列：收合時也看得到目前狀態
  let summaryStatus: ReactNode
  if (auth.loading && status === null) summaryStatus = <span className="muted">檢查中…</span>
  else if (status === null) summaryStatus = <span className="badge badge-failed">無法取得狀態</span>
  else if (status.authenticated)
    summaryStatus = (
      <>
        <span className="badge badge-completed">已登入</span>
        <span>
          <strong>{status.login}</strong>，token 類型：{status.token_type ? TOKEN_TYPE_LABEL[status.token_type] : '未知'}
        </span>
        {status.token_type === 'classic' && (
          <span className="badge badge-importing">⚠ classic 權限過大，建議改用 fine-grained</span>
        )}
      </>
    )
  else if (status.warning !== null)
    summaryStatus = <span className="badge badge-failed">⚠ 已保存的 token 無法使用，請重新貼上</span>
  else summaryStatus = <span className="badge badge-importing">未設定（只能註冊公開 repo）</span>

  return (
    <details className="panel advanced">
      <summary className="summary-status">
        <span>GitHub 連線</span>
        {summaryStatus}
      </summary>
      <ApiHint
        apis={[
          'GET /api/v1/github/auth（狀態）',
          'PUT /api/v1/github/token（驗證並儲存）',
          'DELETE /api/v1/github/auth（刪除）',
        ]}
      />

      <h3>token 狀態</h3>
      {auth.loading && <p role="status">載入中…</p>}
      <ErrorBox error={auth.error} />
      {status && !status.authenticated && (
        <p>
          <span className="badge badge-importing">未設定</span>{' '}
          {status.warning === null && '沒有 token 時只能註冊公開 repo；私有 repo 需要先儲存 token。'}
        </p>
      )}
      {status && status.authenticated && (
        <p>
          <span className="badge badge-completed">已登入</span> GitHub 帳號 <strong>{status.login}</strong>
          ，token 類型：{status.token_type ? TOKEN_TYPE_LABEL[status.token_type] : '未知'}
        </p>
      )}
      {status?.token_type === 'classic' && (
        <div className="warning" role="alert">
          <strong>⚠ classic token 權限過大。</strong>
          classic token 的 <code>repo</code> 範圍涵蓋你所有可存取 repo 的讀寫權限，建議改用只限定特定 repo、只給
          Contents 唯讀的 fine-grained token。
          <div className="muted">後端訊息：{status.warning}</div>
        </div>
      )}
      {status && status.token_type !== 'classic' && status.warning !== null && (
        <div className="warning" role="alert">
          <strong>已保存的 token 無法使用。</strong>
          可能是後端的 TOKEN_ENCRYPTION_KEY 遺失或更換過，請重新貼上 token。
          <div className="muted">後端訊息：{status.warning}</div>
        </div>
      )}

      <h3>貼上 token</h3>
      <form onSubmit={saveToken} className="form">
        <label>
          GitHub personal access token
          <input
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={tokenInput}
            onChange={(e) => setTokenInput(e.target.value)}
            placeholder="github_pat_…"
          />
        </label>
        <p className="note">
          後端會先用這個 token 呼叫 GitHub 驗證，通過後才加密保存；token 不會顯示、不會寫進瀏覽器儲存空間。送出後輸入框會立刻清空。
        </p>
        <div className="toolbar">
          <button type="submit" disabled={busy !== null || tokenInput.trim() === ''}>
            {busy === 'save' ? '驗證中…' : '驗證並儲存'}
          </button>
          <button type="button" className="danger" disabled={busy !== null || !canDelete} onClick={deleteToken}>
            {busy === 'delete' ? '刪除中…' : '刪除 token'}
          </button>
        </div>
      </form>
      <p className="note">
        「刪除 token」只刪除本機（後端資料庫）保存的 token，<strong>不會在 GitHub 端撤銷</strong>
        。要撤銷請到{' '}
        <a href={TOKEN_SETTINGS_URL} target="_blank" rel="noreferrer noopener">
          GitHub token 設定頁
        </a>
        。
      </p>

      {message && (
        <p className="success" role="status">
          {message}
        </p>
      )}
      <ErrorBox error={error} />
      {error?.code === 'token_encryption_not_configured' && (
        <div className="warning">
          <strong>後端尚未設定 token 加密金鑰。</strong>
          <ol>
            <li>
              產生 Fernet 金鑰（指令見 README「GitHub token（切片 5c-1）」的步驟 2），貼到專案根目錄 <code>.env</code>：
              <code>TOKEN_ENCRYPTION_KEY=&lt;金鑰&gt;</code>
            </li>
            <li>重新啟動後端（後端只在啟動時讀取 .env），再回來貼上 token。</li>
          </ol>
        </div>
      )}

      <h3>如何建立 fine-grained token</h3>
      <p className="note">
        公開 repo <strong>不需要 token</strong>。私有 repo 請到{' '}
        <a href={TOKEN_SETTINGS_URL} target="_blank" rel="noreferrer noopener">
          GitHub token 設定頁
        </a>{' '}
        建立 fine-grained token：
      </p>
      <ul className="steps">
        <li>
          <strong>Resource owner</strong>：選擇 repo 所屬的帳號或組織。
        </li>
        <li>
          <strong>Repository access</strong>：只選要使用的 repo。
        </li>
        <li>
          <strong>Permissions</strong>：只給 <code>Contents: Read-only</code>。
        </li>
      </ul>
      <p className="note">
        token 只經 HTTP 送到本機後端，請只在本機使用。私有 repo 匯入後，其內容會存進本機資料庫。
      </p>
    </details>
  )
}
