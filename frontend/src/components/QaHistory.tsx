import { useState } from 'react'
import { api, withQuery } from '../api'
import { toApiError } from '../format'
import { formatTokens, formatUsd } from '../qaFormat'
import { isQaActive, QA_STATUS_BADGE, QA_STATUS_LABEL } from '../qaQuestion'
import type { QaQuestionList, QaQuestionListItem, QaQuestionsDeleted } from '../types'
import { ApiHint, Pager } from '../ui'
import { useAction } from '../useAction'
import { usePolling } from '../usePolling'
import { ConfirmBox } from './ConfirmBox'
import { RelativeTime } from './JobTimes'
import { PollErrorBox } from './PollErrorBox'
import { QaErrorBox } from './QaErrorBox'

const PAGE_SIZE = 10
const QUESTION_PREVIEW_CHARS = 80

interface Props {
  /** 改變就立刻重新讀取（提問、問答結束、刪除之後）。 */
  reloadKey: number
  viewId: number | null
  onView: (id: number) => void
  /** 單筆刪除成功（或早已不存在）。 */
  onDeleted: (id: number) => void
  /** 「刪除全部」完成：被刪的問答 id 後端沒有回報，呼叫端自行決定要不要清掉目前檢視。 */
  onDeletedAll: () => void
}

function preview(text: string): string {
  const chars = [...text]
  return chars.length > QUESTION_PREVIEW_CHARS ? `${chars.slice(0, QUESTION_PREVIEW_CHARS).join('')}…` : text
}

function Row({ item, viewing, onView, onDeleted }: { item: QaQuestionListItem; viewing: boolean; onView: () => void; onDeleted: () => void }) {
  const action = useAction()
  const [confirming, setConfirming] = useState(false)
  const active = isQaActive(item.status)

  async function remove() {
    const ok = await action.run(async () => {
      try {
        await api<void>('DELETE', `/api/v1/qa/questions/${item.id}`)
      } catch (e) {
        // 早已不存在＝目的已達成，當作成功
        if (toApiError(e).code !== 'qa_question_not_found') throw e
      }
    })
    setConfirming(false)
    if (ok) onDeleted()
  }

  return (
    <li className={viewing ? 'qa-history-item current' : 'qa-history-item'}>
      <div className="qa-history-line">
        <strong>#{item.id}</strong>
        <span className={`badge ${QA_STATUS_BADGE[item.status]}`}>{QA_STATUS_LABEL[item.status]}</span>
        {item.answer_status === 'insufficient_evidence' && <span className="badge badge-importing">找不到足夠證據</span>}
        <span className="qa-history-question">{preview(item.question)}</span>
      </div>
      <div className="muted qa-history-meta">
        <code>{item.model}</code>・<RelativeTime iso={item.created_at} label="建立時間" />
        ・{item.usage.input_tokens === null ? '—' : `${formatTokens((item.usage.input_tokens ?? 0) + (item.usage.output_tokens ?? 0))} token`}
        ・{item.cost.actual_usd === null ? `預估 ${formatUsd(item.cost.estimated_usd)} 美元` : `${formatUsd(item.cost.actual_usd)} 美元`}
        {item.error_code !== null && (
          <>
            ・<code>{item.error_code}</code>
          </>
        )}
      </div>
      <div className="action-row">
        <button type="button" className="secondary small" aria-pressed={viewing} onClick={onView}>
          {viewing ? '檢視中' : '查看'}
        </button>
        <button
          type="button"
          className="danger small"
          disabled={active || action.busy}
          aria-expanded={confirming}
          title={active ? '排隊或執行中的問答不能刪除；請先取消，或等它結束' : undefined}
          onClick={() => setConfirming(true)}
        >
          刪除
        </button>
        {active && <span className="muted">排隊或執行中，不能刪除（先取消）</span>}
      </div>
      {confirming && (
        <ConfirmBox
          title={`確定要刪除問答 #${item.id} 嗎？`}
          confirmLabel="確定刪除"
          busy={action.busy}
          onConfirm={() => void remove()}
          onCancel={() => setConfirming(false)}
        >
          <p>會一併刪除它的答案、引用與送出片段的記錄，無法復原。已計入的用量與費用不會因此減少。</p>
        </ConfirmBox>
      )}
      {action.error !== null && <QaErrorBox error={action.error} title="刪除失敗" />}
    </li>
  )
}

/**
 * 歷史問答：新的在前、分頁，可查看、刪除單筆、刪除全部已結束的（兩段式確認，與移除 repo 同樣的作法）。
 * 排隊或執行中的問答不能刪除（後端 409 qa_question_active）；「刪除全部」會保留它們並回報數量。
 */
export function QaHistory({ reloadKey, viewId, onView, onDeleted, onDeletedAll }: Props) {
  const [offset, setOffset] = useState(0)
  const [localReload, setLocalReload] = useState(0)
  const [confirmAll, setConfirmAll] = useState(false)
  const allAction = useAction()
  const path = withQuery('/api/v1/qa/questions', { limit: PAGE_SIZE, offset })
  const poll = usePolling<QaQuestionList>(path, {
    enabled: true,
    reloadKey: `${reloadKey}:${localReload}`,
    intervalMs: (data) => (data !== null && data.items.some((item) => isQaActive(item.status)) ? 3_000 : 30_000),
  })
  const page = poll.data

  // 刪除後目前這一頁可能變空：回到最後一個有資料的頁
  if (page !== null && page.items.length === 0 && page.total > 0 && offset > 0) {
    setOffset(Math.max(0, Math.floor((page.total - 1) / PAGE_SIZE) * PAGE_SIZE))
  }

  async function deleteAll() {
    const ok = await allAction.run(async () => {
      const res = await api<QaQuestionsDeleted>('DELETE', '/api/v1/qa/questions')
      setLocalReload((value) => value + 1)
      onDeletedAll()
      return `已刪除 ${res.data.deleted} 筆已結束的問答${res.data.skipped_active > 0 ? `；${res.data.skipped_active} 筆排隊或執行中的問答保留（先取消再刪）` : ''}。`
    })
    if (ok) setConfirmAll(false)
  }

  return (
    <div className="qa-history">
      <ApiHint apis={['GET /api/v1/qa/questions?limit=&offset=（新的在前）', 'DELETE /api/v1/qa/questions/{id}', 'DELETE /api/v1/qa/questions（所有已結束的）']} />
      {poll.loading && <p role="status">載入歷史問答中…</p>}
      <PollErrorBox state={poll} />
      {page !== null && page.total === 0 && <p className="empty">還沒有任何問答。</p>}
      {page !== null && page.items.length > 0 && (
        <>
          <ul className="qa-history-list">
            {page.items.map((item) => (
              <Row
                key={item.id}
                item={item}
                viewing={item.id === viewId}
                onView={() => onView(item.id)}
                onDeleted={() => {
                  setLocalReload((value) => value + 1)
                  onDeleted(item.id)
                }}
              />
            ))}
          </ul>
          <Pager total={page.total} limit={PAGE_SIZE} offset={offset} onChange={setOffset} />
        </>
      )}
      {page !== null && page.total > 0 && (
        <div className="qa-history-all">
          <button type="button" className="danger" aria-expanded={confirmAll} disabled={allAction.busy} onClick={() => setConfirmAll(true)}>
            刪除全部已結束的問答
          </button>
          {confirmAll && (
            <ConfirmBox
              title="確定要刪除所有已結束的問答嗎？"
              confirmLabel="確定全部刪除"
              busy={allAction.busy}
              onConfirm={() => void deleteAll()}
              onCancel={() => setConfirmAll(false)}
            >
              <p>共 {page.total} 筆問答；排隊或執行中的會保留。刪除會一併移除答案、引用與送出片段的記錄，無法復原；已計入的用量與費用不會減少。</p>
            </ConfirmBox>
          )}
        </div>
      )}
      {allAction.notice !== null && (
        <p className="success" role="status">
          {allAction.notice}
        </p>
      )}
      {allAction.error !== null && <QaErrorBox error={allAction.error} title="刪除失敗" />}
    </div>
  )
}
