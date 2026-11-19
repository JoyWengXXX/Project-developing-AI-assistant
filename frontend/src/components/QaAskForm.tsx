import { api } from '../api'
import { repoTitle } from '../format'
import { formatTokens, formatUsd } from '../qaFormat'
import type { QaQuestionSubmitResponse, QaSettings, QaUsage } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import type { QaConsents } from '../useQaConsents'
import { useLoad } from '../useLoad'
import { QaErrorBox } from './QaErrorBox'
import type { QaSelection } from './QaSnapshotPicker'

/** 問題長度上限（後端 MAX_QUESTION_CHARS；以字元計，不是位元組）。超過時後端回 422，這裡只是讓明顯的錯誤不必送出。 */
export const QA_QUESTION_MAX_CHARS = 2000

interface Props {
  settings: QaSettings | null
  settingsLoading: boolean
  settingsFailed: boolean
  selected: QaSelection[]
  consents: QaConsents
  text: string
  onTextChange: (text: string) => void
  /** 今日額度（GET /qa/usage）要重新讀取時遞增。 */
  usageKey: number
  onRemoveSnapshot: (selection: QaSelection) => void
  onSubmitted: (response: QaQuestionSubmitResponse) => void
  onOpenSettings: () => void
}

/** 為什麼現在不能提問；可以提問時回傳 null。伺服器端的檢查（額度、同意、索引）不在這裡重做。 */
function blockReason(props: Pick<Props, 'settings' | 'settingsLoading' | 'settingsFailed' | 'selected' | 'text'>, chars: number): string | null {
  const { settings } = props
  if (settings === null) return props.settingsFailed ? '無法取得問答設定，不能提問。' : props.settingsLoading ? '正在載入問答設定…' : '尚未取得問答設定。'
  if (!settings.can_ask) {
    if (settings.model === null) return '尚未選擇模型，不能提問。'
    return '尚未設定可用的 API key，不能提問。'
  }
  if (props.selected.length === 0) return '請先選擇至少一個版本。'
  if (props.text.trim() === '') return '請輸入問題。'
  if (chars > QA_QUESTION_MAX_CHARS) return `問題超過 ${QA_QUESTION_MAX_CHARS} 字。`
  return null
}

function UsageSummary({ settings, usageKey }: { settings: QaSettings | null; usageKey: number }) {
  const usage = useLoad<QaUsage>('/api/v1/qa/usage', usageKey)
  const data = usage.data
  const limits = settings?.limits ?? null
  return (
    <div className="qa-usage">
      <h4>今日額度</h4>
      {usage.loading && data === null && <p role="status">載入今日額度中…</p>}
      <ErrorBox error={usage.error} />
      {data !== null && (
        <dl className="kv">
          <dt>今日已用</dt>
          <dd>
            {formatTokens(data.today.total_tokens)} token・{formatUsd(data.today.cost_usd)} 美元
          </dd>
          <dt>今日剩餘</dt>
          <dd>
            {data.remaining_tokens === null
              ? limits !== null && limits.daily_tokens === null ? 'token 不限制' : 'token —'
              : `${formatTokens(data.remaining_tokens)} token`}
            ・
            {data.remaining_cost_usd === null
              ? limits !== null && limits.daily_cost_usd === null ? '費用不限制' : '費用 —'
              : `${formatUsd(data.remaining_cost_usd)} 美元`}
          </dd>
          {limits !== null && (
            <>
              <dt>單次上限</dt>
              <dd>{limits.per_request_tokens === null ? '不限制' : `${formatTokens(limits.per_request_tokens)} token`}</dd>
            </>
          )}
        </dl>
      )}
      <p className="note">額度只是 RepoPilot 自己的上限；超過時由伺服器拒絕，不會呼叫供應商。</p>
    </div>
  )
}

function EstimatePreview({ settings }: { settings: QaSettings | null }) {
  const option = settings?.models.find((item) => item.id === settings.model) ?? null
  return (
    <div className="qa-estimate">
      <h4>提交前的預估</h4>
      {settings === null || option === null ? (
        <p className="muted">選擇模型後才能預估。</p>
      ) : (
        <p>
          每題約 <strong>{formatUsd(option.estimated_cost_per_question_usd)} 美元</strong>（<code>{option.id}</code>；假設輸入{' '}
          {formatTokens(settings.estimate_basis.input_tokens)}、輸出 {formatTokens(settings.estimate_basis.output_tokens)} token，單價每百萬
          token 輸入 {formatUsd(option.price.input_usd_per_million)}／輸出 {formatUsd(option.price.output_usd_per_million)} 美元）。
          這是假設值，不是實測；送出後會顯示這題的上限估計，結束後顯示實際用量。
        </p>
      )}
    </div>
  )
}

/**
 * 提問表單：已選版本、問題（最多 2000 字）、提交前的預估與今日額度。
 * can_ask 為 false（沒選模型、沒有可用的 API key）時提交鈕停用並說明原因，附「前往問答設定」。
 * 問題只存在記憶體（不寫入 localStorage／sessionStorage／URL）；額度、同意、索引是否足夠由伺服器決定，錯誤依錯誤碼顯示。
 */
export function QaAskForm(props: Props) {
  const { settings, selected, consents, text, onTextChange } = props
  const action = useAction()
  const chars = [...text].length
  const reason = blockReason(props, chars)
  const consentKnown = consents.byRepo !== null
  const unconsented = consentKnown
    ? [...new Map(selected.map((item) => [item.repo.id, item.repo])).values()].filter((repo) => !consents.byRepo?.has(repo.id))
    : []

  async function submit() {
    if (reason !== null) return
    await action.run(async () => {
      const res = await api<QaQuestionSubmitResponse>('POST', '/api/v1/qa/questions', {
        body: { snapshot_ids: selected.map((item) => item.snapshot.id), question: text },
      })
      onTextChange('')
      props.onSubmitted(res.data)
    })
  }

  return (
    <form
      className="qa-ask"
      noValidate
      onSubmit={(event) => {
        event.preventDefault()
        void submit()
      }}
    >
      <h4>已選版本（{selected.length}）</h4>
      {selected.length === 0 && <p className="empty">尚未選取版本。</p>}
      <ul className="qa-selected">
        {selected.map((item) => (
          <li key={item.snapshot.id}>
            <strong>{repoTitle(item.repo)}</strong>・快照 #{item.snapshot.id}・<code className="wrap">{item.snapshot.commit_sha}</code>{' '}
            <button type="button" className="secondary small" onClick={() => props.onRemoveSnapshot(item)}>
              移除選取
            </button>
          </li>
        ))}
      </ul>
      {unconsented.length > 0 && (
        <p className="warning" role="status">
          {unconsented.map((repo) => `「${repoTitle(repo)}」`).join('、')} 尚未同意把程式片段送往供應商；現在提問會被伺服器拒絕。請到「倉庫總覽」該 repo 卡片的「問答資料外送」區同意（或在「問答設定」的同意清單操作）。
        </p>
      )}

      <label>
        問題（最多 {QA_QUESTION_MAX_CHARS} 字）
        <textarea
          rows={4}
          maxLength={QA_QUESTION_MAX_CHARS * 2}
          value={text}
          aria-invalid={chars > QA_QUESTION_MAX_CHARS}
          aria-describedby="qa-question-count"
          onChange={(event) => onTextChange(event.target.value)}
        />
      </label>
      <p id="qa-question-count" className={chars > QA_QUESTION_MAX_CHARS ? 'field-error' : 'muted'}>
        {chars.toLocaleString('zh-TW')} / {QA_QUESTION_MAX_CHARS.toLocaleString('zh-TW')} 字
      </p>

      <div className="two-col">
        <EstimatePreview settings={settings} />
        <UsageSummary settings={settings} usageKey={props.usageKey} />
      </div>

      <div className="toolbar">
        <button type="submit" disabled={reason !== null || action.busy} aria-describedby={reason !== null ? 'qa-submit-reason' : undefined}>
          {action.busy ? '送出中…' : '送出問題'}
        </button>
        {settings !== null && !settings.can_ask && (
          <button type="button" className="secondary" onClick={props.onOpenSettings}>
            前往問答設定
          </button>
        )}
      </div>
      {reason !== null && (
        <p id="qa-submit-reason" className="note" role="status">
          無法送出：{reason}
        </p>
      )}
      <QaErrorBox error={action.error} title="無法提問" />
    </form>
  )
}
