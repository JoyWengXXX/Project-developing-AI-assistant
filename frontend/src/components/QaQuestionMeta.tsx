import { formatTime } from '../format'
import { formatTokens, formatUsd } from '../qaFormat'
import { durationText, QA_STATUS_BADGE, QA_STATUS_LABEL, skipLabel } from '../qaQuestion'
import type { QaQuestion } from '../types'
import type { SnapshotPresence } from '../useSnapshotPresence'
import { QaSourceRef } from './QaSourceRef'

type PresenceMap = ReadonlyMap<number, SnapshotPresence> | null

function tokenText(value: number | null): string {
  return value === null ? '—' : formatTokens(value)
}

/** 費用、用量與追蹤資訊：預估與實際費用、token、嘗試次數、耗時、模型、提示詞版本。 */
export function QaQuestionMeta({ question }: { question: QaQuestion }) {
  const { usage, cost } = question
  const sent = question.context_chunks.filter((chunk) => chunk.code !== null).length
  return (
    <dl className="kv">
      <dt>狀態</dt>
      <dd>
        <span className={`badge ${QA_STATUS_BADGE[question.status]}`}>{QA_STATUS_LABEL[question.status]}</span>
      </dd>
      <dt>模型</dt>
      <dd>
        <code>{question.model}</code>（{question.provider}）
      </dd>
      <dt>提示詞版本</dt>
      <dd>
        <code>{question.prompt_version}</code>
      </dd>
      <dt>預估費用</dt>
      <dd>
        {formatUsd(cost.estimated_usd)} 美元（上限估計：輸入約 {formatTokens(cost.estimated_input_tokens)} token、輸出最多{' '}
        {formatTokens(question.max_tokens)} token；假設值，不是實測）
      </dd>
      <dt>實際費用</dt>
      <dd>{cost.actual_usd === null ? '—（尚未結算）' : `${formatUsd(cost.actual_usd)} 美元`}</dd>
      <dt>實際 token</dt>
      <dd>
        輸入 {tokenText(usage.input_tokens)}・輸出 {tokenText(usage.output_tokens)}
        {usage.usage_unknown && '（供應商沒有回報用量，帳本沿用預留量）'}
      </dd>
      <dt>供應商呼叫次數</dt>
      <dd>{question.attempts}（含重試；token 與費用是各次的累計）</dd>
      <dt>耗時</dt>
      <dd>{durationText(question.duration_ms)}</dd>
      <dt>時間</dt>
      <dd>
        建立 {formatTime(question.created_at)}・開始 {formatTime(question.started_at)}・結束 {formatTime(question.finished_at)}
      </dd>
      <dt>選取的快照</dt>
      <dd>{question.snapshot_ids.map((id) => `#${id}`).join('、')}</dd>
      <dt>送出的片段</dt>
      <dd>
        {sent} 個已送出{question.context_chunks.length - sent > 0 && `・${question.context_chunks.length - sent} 個檢索到但未送出`}
      </dd>
      {question.provider_request_id !== null && (
        <>
          <dt>供應商 request id</dt>
          <dd>
            <code className="wrap">{question.provider_request_id}</code>
          </dd>
        </>
      )}
      {question.job_id !== null && (
        <>
          <dt>背景工作</dt>
          <dd>#{question.job_id}</dd>
        </>
      )}
    </dl>
  )
}

/** 檢索到的片段：已送出的帶代號（引用用的 C1、C2…），沒送出的說明原因。 */
export function QaContextChunks({ question, presence }: { question: QaQuestion; presence: PresenceMap }) {
  const chunks = question.context_chunks
  if (chunks.length === 0) return <p className="empty">沒有片段記錄（問題可能在檢索前就結束了）。</p>
  return (
    <ol className="qa-chunks">
      {chunks.map((chunk) => (
        <li key={chunk.rank} className={chunk.code === null ? 'qa-chunk skipped' : 'qa-chunk'}>
          <div className="muted">
            排名 {chunk.rank}
            {chunk.truncated && '・內容過長，只送出前面一段'}
            {chunk.skipped_reason !== null && `・${skipLabel(chunk.skipped_reason)}`}
          </div>
          <QaSourceRef source={chunk} code={chunk.code ?? undefined} presence={presence?.get(chunk.snapshot_id)} />
        </li>
      ))}
    </ol>
  )
}
