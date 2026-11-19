import { useState } from 'react'
import { api } from '../api'
import { formatUsd, TIER_LABEL } from '../qaFormat'
import type { QaSettings } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'

interface Props {
  settings: QaSettings
  onSaved: (next: QaSettings) => void
}

/**
 * 模型選擇：三個選項（低、中、高階）一律列出，每個顯示單價與每題預估；沒有預設值，第一次使用必須自己選。
 * 選了之後要按「儲存」才生效（選項只是草稿，沒儲存前上方仍顯示「無法提問」）。
 */
export function QaModelChooser({ settings, onSaved }: Props) {
  const action = useAction()
  const [draft, setDraft] = useState<string | null>(null) // null：沒有未儲存的選擇
  const selected = draft ?? settings.model
  const dirty = draft !== null && draft !== settings.model
  const basis = settings.estimate_basis

  async function save() {
    if (draft === null) return
    await action.run(async () => {
      const res = await api<QaSettings>('PUT', '/api/v1/qa/settings', { body: { model: draft } })
      onSaved(res.data)
      setDraft(null)
      return '已儲存模型選擇。'
    })
  }

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <fieldset className="device-picker qa-models">
        <legend>選擇模型（沒有預設，請自己選一個）</legend>
        {settings.models.map((model) => (
          <label className="qa-model" key={model.id}>
            <input
              type="radio"
              name="qa-model"
              value={model.id}
              checked={selected === model.id}
              disabled={action.busy}
              onChange={() => setDraft(model.id)}
            />
            <span className="qa-model-body">
              <span>
                <strong>{TIER_LABEL[model.tier]}階</strong>・<code>{model.id}</code>
                {settings.model === model.id && <span className="badge badge-completed qa-model-badge">目前使用</span>}
              </span>
              <span className="muted">
                單價（每百萬 token）：輸入 {formatUsd(model.price.input_usd_per_million)}・輸出{' '}
                {formatUsd(model.price.output_usd_per_million)}（美元）
              </span>
              <span>
                每題預估約 <strong>{formatUsd(model.estimated_cost_per_question_usd)}</strong> 美元
                <span className="muted">
                  （假設每題輸入 {basis.input_tokens.toLocaleString('zh-TW')} token、輸出{' '}
                  {basis.output_tokens.toLocaleString('zh-TW')} token，依上列單價估算；不是實測）
                </span>
              </span>
              {model.price_note && <span className="muted">價格備註：{model.price_note}</span>}
            </span>
          </label>
        ))}
      </fieldset>
      <div className="toolbar">
        <button type="submit" disabled={action.busy || !dirty}>
          {action.busy ? '儲存中…' : '儲存模型選擇'}
        </button>
        {draft === null && settings.model === null && <span className="muted">尚未選擇</span>}
      </div>
      {action.notice && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} />
    </form>
  )
}
