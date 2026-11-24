import { useState } from 'react'
import type { ApiError } from '../api'
import type { HealthResponse, ReadyResponse } from '../types'
import { useLoad } from '../useLoad'
import { useTheme } from '../useTheme'
import type { UiMode } from '../useUiMode'
import { ApiHint } from '../ui'

function StatusDot(props: { label: string; loading: boolean; ok: boolean; okText: string; error: ApiError | null }) {
  const { label, loading, ok, okText, error } = props
  let text = '檢查中'
  let state = 'pending'
  if (!loading && ok) {
    text = okText
    state = 'ok'
  } else if (!loading && error) {
    text = '無法連線'
    state = 'bad'
  }
  const title = error !== null && !loading ? `${error.code}${error.status !== null ? `，HTTP ${error.status}` : ''}` : undefined
  return (
    <span className="status-dot" data-state={state} title={title}>
      {label} <strong>{text}</strong>
    </span>
  )
}

/** 頁首：品牌、一般／開發者模式切換、後端與資料庫狀態、主題。 */
export function AppHeader({ mode, onModeChange }: { mode: UiMode; onModeChange: (mode: UiMode) => void }) {
  const [checkKey, setCheckKey] = useState(0)
  const health = useLoad<HealthResponse>('/health', checkKey)
  const ready = useLoad<ReadyResponse>('/ready', checkKey)
  const { theme, toggle } = useTheme()
  const failed = (!health.loading && health.error !== null) || (!ready.loading && ready.error !== null)

  return (
    <header className="app-header">
      <div className="brand">
        <span className="brand-name">RepoPilot</span>
        <span className="brand-tag">以參考專案畫出你的新系統</span>
      </div>
      <div className="mode-switch" role="radiogroup" aria-label="介面模式">
        {(['general', 'developer'] as const).map((value) => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={mode === value}
            tabIndex={mode === value ? 0 : -1}
            onClick={() => onModeChange(value)}
            onKeyDown={(e) => {
              if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown'].includes(e.key)) {
                e.preventDefault()
                const next = value === 'general' ? 'developer' : 'general'
                onModeChange(next)
                const sibling = e.currentTarget.parentElement?.querySelector<HTMLButtonElement>(`[data-mode="${next}"]`)
                sibling?.focus()
              }
            }}
            data-mode={value}
          >
            {value === 'general' ? '一般模式' : '開發者模式'}
          </button>
        ))}
      </div>
      <div className="header-status" aria-live="polite">
        <StatusDot label="後端" loading={health.loading} ok={health.data?.status === 'ok'} okText="正常" error={health.error} />
        <StatusDot label="資料庫" loading={ready.loading} ok={ready.data?.status === 'ready'} okText="就緒" error={ready.error} />
        {failed && (
          <button type="button" className="secondary small" onClick={() => setCheckKey((k) => k + 1)}>
            重新檢查
          </button>
        )}
        <button type="button" className="secondary small" onClick={toggle}>
          {theme === 'dark' ? '切換為淺色' : '切換為深色'}
        </button>
        <ApiHint apis={['GET /health', 'GET /ready']} />
      </div>
    </header>
  )
}
