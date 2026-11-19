import { useState } from 'react'
import { api } from '../api'
import { parsePrice, sameDecimal, formatUsd, TIER_LABEL } from '../qaFormat'
import type { QaModelPrice, QaSettings } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'

interface Props {
  settings: QaSettings
  onSaved: (next: QaSettings) => void
}

type Drafts = Record<string, Partial<Record<keyof QaModelPrice, string>>>

const savedText = (value: string) => formatUsd(value).slice(1)

/**
 * 單價表（每百萬 token 的美元單價）：預設值取自官方價格頁並標註讀取日期，價格常變，使用者可自行更新。
 * 儲存後「每題預估」與之後的費用換算都使用新單價。只送出有改的模型。
 * 欄位的草稿沒有值代表「沿用已儲存的值」；儲存成功後草稿清空。
 */
export function QaPriceTable({ settings, onSaved }: Props) {
  const action = useAction()
  const [drafts, setDrafts] = useState<Drafts>({})
  const [submitted, setSubmitted] = useState(false)

  function value(modelId: string, field: keyof QaModelPrice, saved: string): string {
    return drafts[modelId]?.[field] ?? savedText(saved)
  }

  function edit(modelId: string, field: keyof QaModelPrice, text: string) {
    setDrafts((current) => ({ ...current, [modelId]: { ...current[modelId], [field]: text } }))
  }

  const rows = settings.models.map((model) => {
    const inText = value(model.id, 'input_usd_per_million', model.price.input_usd_per_million)
    const outText = value(model.id, 'output_usd_per_million', model.price.output_usd_per_million)
    return {
      model,
      inText,
      outText,
      inParsed: parsePrice(inText),
      outParsed: parsePrice(outText),
      changed:
        !sameDecimal(inText, model.price.input_usd_per_million) ||
        !sameDecimal(outText, model.price.output_usd_per_million),
    }
  })
  const changedRows = rows.filter((row) => row.changed)

  async function save() {
    setSubmitted(true)
    const priceTable: Record<string, QaModelPrice> = {}
    for (const row of changedRows) {
      if (!row.inParsed.ok || !row.outParsed.ok) return
      priceTable[row.model.id] = {
        input_usd_per_million: row.inParsed.value,
        output_usd_per_million: row.outParsed.value,
      }
    }
    if (changedRows.length === 0) return
    await action.run(async () => {
      const res = await api<QaSettings>('PUT', '/api/v1/qa/settings', { body: { price_table: priceTable } })
      onSaved(res.data)
      setDrafts({})
      setSubmitted(false)
      return '已儲存單價；模型旁的每題預估已改用新單價。'
    })
  }

  return (
    <form
      noValidate
      onSubmit={(e) => {
        e.preventDefault()
        void save()
      }}
    >
      <p>
        單價讀取日期：<strong>{settings.price_table_as_of}</strong>
        <span className="muted">（儲存單價後會更新為儲存當天）</span>
      </p>
      <table className="table compact qa-price-table">
        <thead>
          <tr>
            <th scope="col">模型</th>
            <th scope="col">輸入（美元／百萬 token）</th>
            <th scope="col">輸出（美元／百萬 token）</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.model.id}>
              <th scope="row">
                {TIER_LABEL[row.model.tier]}階・<code>{row.model.id}</code>
              </th>
              <td>
                <input
                  type="text"
                  inputMode="decimal"
                  autoComplete="off"
                  aria-label={`${row.model.id} 輸入單價（美元／百萬 token）`}
                  value={row.inText}
                  aria-invalid={submitted && !row.inParsed.ok}
                  onChange={(e) => edit(row.model.id, 'input_usd_per_million', e.target.value)}
                />
                {submitted && !row.inParsed.ok && <div className="field-error" role="alert">{row.inParsed.message}</div>}
              </td>
              <td>
                <input
                  type="text"
                  inputMode="decimal"
                  autoComplete="off"
                  aria-label={`${row.model.id} 輸出單價（美元／百萬 token）`}
                  value={row.outText}
                  aria-invalid={submitted && !row.outParsed.ok}
                  onChange={(e) => edit(row.model.id, 'output_usd_per_million', e.target.value)}
                />
                {submitted && !row.outParsed.ok && <div className="field-error" role="alert">{row.outParsed.message}</div>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="toolbar">
        <button type="submit" disabled={action.busy || changedRows.length === 0}>
          {action.busy ? '儲存中…' : '儲存單價'}
        </button>
        {changedRows.length > 0 && (
          <button type="button" className="secondary" disabled={action.busy} onClick={() => { setDrafts({}); setSubmitted(false) }}>
            放棄修改
          </button>
        )}
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
