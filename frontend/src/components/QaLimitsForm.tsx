import { useState } from 'react'
import { api } from '../api'
import { formatTime } from '../format'
import {
  DAILY_TOKENS_MAX, formatTokens, formatUsd, parseCostLimit, parseTokenLimit, PER_REQUEST_MAX,
} from '../qaFormat'
import type { QaSettings } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'

interface Props {
  settings: QaSettings
  onSaved: (next: QaSettings) => void
}

function UsageSummary({ settings }: { settings: QaSettings }) {
  const { usage, limits } = settings
  return (
    <>
      <h4>今日用量與剩餘額度</h4>
      <dl className="kv">
        <dt>今日已用 token</dt>
        <dd>
          {formatTokens(usage.today.total_tokens)}（輸入 {formatTokens(usage.today.input_tokens)}・輸出{' '}
          {formatTokens(usage.today.output_tokens)}）
        </dd>
        <dt>今日已用費用</dt>
        <dd>{formatUsd(usage.today.cost_usd)} 美元</dd>
        <dt>今日剩餘 token</dt>
        <dd>
          {usage.remaining_tokens === null
            ? limits.daily_tokens === null ? '不限制' : '—'
            : formatTokens(usage.remaining_tokens)}
        </dd>
        <dt>今日剩餘費用</dt>
        <dd>
          {usage.remaining_cost_usd === null
            ? limits.daily_cost_usd === null ? '不限制' : '—'
            : `${formatUsd(usage.remaining_cost_usd)} 美元`}
        </dd>
        <dt>今日視窗</dt>
        <dd>
          {formatTime(usage.window_start)} 起（{formatTime(usage.window_end)} 止）
          {usage.last_reset_at !== null && `・上次手動重置 ${formatTime(usage.last_reset_at)}`}
        </dd>
        <dt>累計</dt>
        <dd>
          {formatTokens(usage.cumulative.total_tokens)} token・{formatUsd(usage.cumulative.cost_usd)} 美元
        </dd>
      </dl>
    </>
  )
}

/**
 * 額度上限：單次 token、每日 token、每日費用（美元）；留空＝不限制。
 * 欄位的草稿是 null 代表「沿用已儲存的值」；儲存成功後草稿清回 null，欄位就顯示新的已儲存值。
 */
export function QaLimitsForm({ settings, onSaved }: Props) {
  const action = useAction()
  const { limits } = settings
  const [perRequestDraft, setPerRequest] = useState<string | null>(null)
  const [dailyTokensDraft, setDailyTokens] = useState<string | null>(null)
  const [dailyCostDraft, setDailyCost] = useState<string | null>(null)
  const [submitted, setSubmitted] = useState(false)
  const perRequest = perRequestDraft ?? limits.per_request_tokens?.toString() ?? ''
  const dailyTokens = dailyTokensDraft ?? limits.daily_tokens?.toString() ?? ''
  const dailyCost = dailyCostDraft ?? (limits.daily_cost_usd === null ? '' : formatUsd(limits.daily_cost_usd).slice(1))

  const perRequestParsed = parseTokenLimit(perRequest, PER_REQUEST_MAX)
  const dailyTokensParsed = parseTokenLimit(dailyTokens, DAILY_TOKENS_MAX)
  const dailyCostParsed = parseCostLimit(dailyCost)
  const showErrors = submitted

  async function save() {
    setSubmitted(true)
    if (!perRequestParsed.ok || !dailyTokensParsed.ok || !dailyCostParsed.ok) return
    const body = {
      per_request_tokens: perRequestParsed.value,
      daily_tokens: dailyTokensParsed.value,
      daily_cost_usd: dailyCostParsed.value,
    }
    await action.run(async () => {
      const res = await api<QaSettings>('PUT', '/api/v1/qa/settings', { body })
      onSaved(res.data)
      setPerRequest(null)
      setDailyTokens(null)
      setDailyCost(null)
      setSubmitted(false)
      return '已儲存額度。'
    })
  }

  return (
    <>
    <form
      className="form"
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <label>
        單次請求 token 上限
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={perRequest}
          placeholder="留空＝不限制"
          aria-invalid={showErrors && !perRequestParsed.ok}
          onChange={(e) => setPerRequest(e.target.value)}
        />
        {showErrors && !perRequestParsed.ok && <span className="field-error" role="alert">{perRequestParsed.message}</span>}
      </label>
      <label>
        每日 token 上限
        <input
          type="text"
          inputMode="numeric"
          autoComplete="off"
          value={dailyTokens}
          placeholder="留空＝不限制"
          aria-invalid={showErrors && !dailyTokensParsed.ok}
          onChange={(e) => setDailyTokens(e.target.value)}
        />
        {showErrors && !dailyTokensParsed.ok && <span className="field-error" role="alert">{dailyTokensParsed.message}</span>}
      </label>
      <label>
        每日費用上限（美元）
        <input
          type="text"
          inputMode="decimal"
          autoComplete="off"
          value={dailyCost}
          placeholder="留空＝不限制"
          aria-invalid={showErrors && !dailyCostParsed.ok}
          onChange={(e) => setDailyCost(e.target.value)}
        />
        {showErrors && !dailyCostParsed.ok && <span className="field-error" role="alert">{dailyCostParsed.message}</span>}
      </label>
      <p className="note">三個欄位留空都代表不限制；儲存時三個欄位一起送出。</p>
      <div className="toolbar">
        <button type="submit" disabled={action.busy}>
          {action.busy ? '儲存中…' : '儲存額度'}
        </button>
      </div>
      {action.notice && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} />
    </form>
    <UsageSummary settings={settings} />
    </>
  )
}

