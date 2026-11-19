import type { ReactNode } from 'react'
import { formatTime } from '../format'
import type { QaSettings, Repository } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import type { QaConsents } from '../useQaConsents'
import { useQaSettings } from '../useQaSettings'
import { QaConsentList } from './QaConsentList'
import { QaLimitsForm } from './QaLimitsForm'
import { QaModelChooser } from './QaModelChooser'
import { QaPriceTable } from './QaPriceTable'
import { QaProviderKeyForm } from './QaProviderKeyForm'

/** 目前能不能提問：沒有預設模型，沒選模型或沒有可用的 key 都不能問（逐 repo 的同意另外在 repo 卡片）。 */
function AskStatus({ settings }: { settings: QaSettings }) {
  if (settings.can_ask) {
    return (
      <div className="success" role="status">
        <strong>已選擇模型 <code>{settings.model}</code>，API key 可用。</strong>
        <p className="note">每個 repo 還需要個別同意「把程式片段送往 Anthropic API」（見下方清單或 repo 卡片）。</p>
      </div>
    )
  }
  const noModel = settings.model === null
  return (
    <div className="warning" role="status">
      <strong>{noModel ? '尚未選擇模型，無法提問' : '尚未設定可用的 API key，無法提問'}</strong>
      <ul>
        {noModel && <li>沒有預設模型：請在下方選擇低、中、高階其中一個並儲存。</li>}
        {!settings.provider_key_configured && (
          <li>
            {settings.provider_key_warning !== null
              ? '已保存的 API key 無法使用，請重新貼上。'
              : '尚未設定供應商 API key：請在下方貼上。'}
          </li>
        )}
      </ul>
    </div>
  )
}

function SummaryStatus({ settings, loading, failed }: { settings: QaSettings | null; loading: boolean; failed: boolean }): ReactNode {
  if (settings === null) {
    if (loading) return <span className="muted">檢查中…</span>
    return failed ? <span className="badge badge-failed">無法取得狀態</span> : null
  }
  if (settings.can_ask) {
    return <span className="badge badge-completed">可提問（{settings.model}）</span>
  }
  if (settings.model === null) return <span className="badge badge-importing">尚未選擇模型，無法提問</span>
  return <span className="badge badge-importing">尚未設定可用的 API key，無法提問</span>
}

/**
 * 問答設定面板：模型選擇（含單價與每題預估）、額度與今日用量、單價表、供應商 API key、逐 repo 資料外送同意。
 * 這一版只負責「設定」：不呼叫供應商，也沒有問答流程。
 * 設定資料來自 GET /qa/settings（永遠不含 API key）；各區塊儲存後直接採用 PUT 回傳的完整設定。
 */
export function QaSettingsPanel({ repos, consents }: { repos: Repository[] | null; consents: QaConsents }) {
  const state = useQaSettings()
  const { settings } = state

  return (
    <details className="panel advanced">
      <summary className="summary-status">
        <span>問答設定</span>
        <SummaryStatus settings={settings} loading={state.loading} failed={state.error !== null} />
      </summary>
      <ApiHint
        apis={[
          'GET /api/v1/qa/settings（設定；不含 API key）',
          'PUT /api/v1/qa/settings（模型、額度、單價表）',
          'PUT /api/v1/qa/provider-key（只寫；加密保存）',
          'DELETE /api/v1/qa/provider-key（清除）',
          'GET /api/v1/qa/consents（各 repo 的外送同意）',
          'PUT／DELETE /api/v1/repositories/{id}/qa-consent（同意／撤回）',
        ]}
      />

      {state.loading && settings === null && <p role="status">載入問答設定中…</p>}
      <ErrorBox error={state.error} />
      {state.error !== null && (
        <button type="button" className="secondary" disabled={state.loading} onClick={state.reload}>
          {state.loading ? '載入中…' : '重新載入設定'}
        </button>
      )}

      {settings !== null && (
        <>
          <AskStatus settings={settings} />
          {settings.updated_at === null && <p className="note">尚未儲存過任何問答設定（目前顯示的是預設單價與不限額度）。</p>}
          {settings.updated_at !== null && <p className="note">設定最後更新：{formatTime(settings.updated_at)}</p>}

          <h3>模型</h3>
          <QaModelChooser settings={settings} onSaved={state.apply} />

          <h3>額度</h3>
          <QaLimitsForm settings={settings} onSaved={state.apply} />

          <h3>單價表</h3>
          <QaPriceTable settings={settings} onSaved={state.apply} />

          <h3>供應商 API key</h3>
          <QaProviderKeyForm settings={settings} onSaved={state.apply} onCleared={state.reload} />

          <h3>各 repo 的資料外送同意</h3>
          <QaConsentList repos={repos} consents={consents} />
        </>
      )}
    </details>
  )
}
