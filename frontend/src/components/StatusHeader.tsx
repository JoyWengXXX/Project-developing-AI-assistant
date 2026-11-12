import { useState } from 'react'
import type { ApiError } from '../api'
import type { HealthResponse, ReadyResponse } from '../types'
import { useLoad } from '../useLoad'
import { useTheme } from '../useTheme'
import { ApiHint } from '../ui'

function StatusLine(props: {
  label: string
  loading: boolean
  ok: boolean
  okText: string
  error: ApiError | null
}) {
  const { label, loading, ok, okText, error } = props
  let text = '檢查中…'
  let cls = 'badge badge-importing'
  if (!loading && ok) {
    text = okText
    cls = 'badge badge-completed'
  } else if (!loading && error) {
    text = `異常（${error.code}${error.status !== null ? `，HTTP ${error.status}` : ''}）`
    cls = 'badge badge-failed'
  }
  return (
    <span className="status-line">
      {label}：<span className={cls}>{text}</span>
    </span>
  )
}

/** 頁首：後端連線狀態。 */
export function StatusHeader() {
  const [checkKey, setCheckKey] = useState(0)
  const health = useLoad<HealthResponse>('/health', checkKey)
  const ready = useLoad<ReadyResponse>('/ready', checkKey)
  const { theme, toggle } = useTheme()

  return (
    <header className="app-header">
      <div>
        <h1>RepoPilot 操作主控台</h1>
        <p className="subtitle">
          連動 GitHub 或本機 repo（註冊時自動匯入一次預設分支最新版本）→ 在詳情的 commit 圖選版本、按「匯入此版本」→
          回總覽把已匯入的版本設為參考版本（目前只是標記，尚未連到 Agent）→ 不需要的版本可以移除
        </p>
      </div>
      <div className="header-status" aria-live="polite">
        <StatusLine
          label="後端程序"
          loading={health.loading}
          ok={health.data?.status === 'ok'}
          okText="正常"
          error={health.error}
        />
        <StatusLine
          label="資料庫"
          loading={ready.loading}
          ok={ready.data?.status === 'ready'}
          okText="就緒"
          error={ready.error}
        />
        <button type="button" onClick={() => setCheckKey((k) => k + 1)}>
          重新檢查
        </button>
        <button type="button" onClick={toggle}>
          {theme === 'dark' ? '☀ 淺色模式' : '🌙 暗黑模式'}
        </button>
        <ApiHint apis={['GET /health', 'GET /ready']} />
      </div>
    </header>
  )
}
