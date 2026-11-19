import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { QaSettings } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'

interface Props {
  settings: QaSettings
  onSaved: (next: QaSettings) => void
  /** 清除成功後重新讀取設定（DELETE 回 204，沒有內容）。 */
  onCleared: () => void
}

const KEY_ERROR_HINTS: Record<string, string> = {
  token_encryption_not_configured:
    '後端沒有設定 TOKEN_ENCRYPTION_KEY，無法加密，所以這把 API key 沒有被保存（資料庫完全沒有變動）。',
  validation_error: 'API key 格式不合法（不可為空、不可含空白或不可見字元、最長 512 字元）。key 沒有被保存。',
}

/**
 * 供應商 API key：只寫不讀。
 * - key 只存在輸入框的 state。按下送出的瞬間就清空輸入框，請求用的字串只活在 save() 的區域變數；
 *   不寫入 localStorage／sessionStorage、cookie、全域變數、console、URL，也不放進錯誤訊息。
 * - 後端永遠不回傳 key；這裡只顯示「已設定」與後端的警告（例如加密金鑰更換導致無法解密）。
 */
export function QaProviderKeyForm({ settings, onSaved, onCleared }: Props) {
  const action = useAction()
  const [keyInput, setKeyInput] = useState('')

  async function save(e: FormEvent) {
    e.preventDefault()
    if (action.busy) return
    const apiKey = keyInput
    setKeyInput('') // 不論成功或失敗，送出後立即清空輸入框
    await action.run(async () => {
      const res = await api<QaSettings>('PUT', '/api/v1/qa/provider-key', { body: { api_key: apiKey } })
      onSaved(res.data)
      return 'API key 已加密保存。'
    })
  }

  async function clearKey() {
    if (action.busy) return
    if (!window.confirm('確定要清除本機保存的 API key 嗎？\n\n清除後無法提問，直到重新貼上 key。這不會在供應商端撤銷 key。')) return
    setKeyInput('')
    const ok = await action.run(async () => {
      await api<void>('DELETE', '/api/v1/qa/provider-key')
      // 立刻反映在畫面上，再向後端對齊
      onSaved({ ...settings, provider_key_configured: false, provider_key_warning: null, can_ask: false })
      return '已清除本機保存的 API key。'
    })
    if (ok) onCleared()
  }

  const canClear = settings.provider_key_configured || settings.provider_key_warning !== null

  return (
    <>
      <p>
        供應商：Anthropic・狀態：
        {settings.provider_key_configured ? (
          <span className="badge badge-completed">已設定</span>
        ) : (
          <span className="badge badge-importing">尚未設定</span>
        )}
      </p>
      {settings.provider_key_warning && (
        <div className="warning" role="alert">
          <strong>已保存的 API key 無法使用，請重新貼上。</strong>
          可能是後端的 TOKEN_ENCRYPTION_KEY 遺失或更換過。
          <div className="muted">後端訊息：{settings.provider_key_warning}</div>
        </div>
      )}
      <form onSubmit={save} className="form">
        <label>
          Anthropic API key
          <input
            type="password"
            name="qa-provider-api-key"
            autoComplete="new-password"
            spellCheck={false}
            value={keyInput}
            onChange={(e) => setKeyInput(e.target.value)}
            placeholder={settings.provider_key_configured ? '已設定；貼上新的 key 會覆蓋' : 'sk-ant-…'}
          />
        </label>
        <p className="note">
          後端會用 TOKEN_ENCRYPTION_KEY 加密後才保存；key 只寫入、永遠不會顯示或讀回，也不會寫進瀏覽器儲存空間。送出後輸入框會立刻清空。
        </p>
        <div className="toolbar">
          <button type="submit" disabled={action.busy || keyInput.trim() === ''}>
            {action.busy ? '處理中…' : '加密並儲存'}
          </button>
          <button type="button" className="danger" disabled={action.busy || !canClear} onClick={() => void clearKey()}>
            清除 API key
          </button>
        </div>
      </form>
      {action.notice && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} hints={KEY_ERROR_HINTS} />
      {action.error?.code === 'token_encryption_not_configured' && (
        <div className="warning">
          <strong>後端尚未設定 token 加密金鑰。</strong>
          <ol>
            <li>
              產生 Fernet 金鑰（指令見 README「GitHub token（切片 5c-1）」的步驟 2），貼到專案根目錄 <code>.env</code>：
              <code>TOKEN_ENCRYPTION_KEY=&lt;金鑰&gt;</code>
            </li>
            <li>重新啟動後端（後端只在啟動時讀取 .env），再回來貼上 API key。</li>
          </ol>
        </div>
      )}
    </>
  )
}
