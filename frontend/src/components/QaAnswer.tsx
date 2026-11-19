import { QA_KIND_BADGE, QA_KIND_LABEL, rejectionLabel } from '../qaQuestion'
import type { QaCitation, QaQuestion, QaRejectedStatement, QaStatement } from '../types'
import type { SnapshotPresence } from '../useSnapshotPresence'
import { QaSourceRef } from './QaSourceRef'

type PresenceMap = ReadonlyMap<number, SnapshotPresence> | null

const KIND_PREFIX: Record<QaStatement['kind'], string | null> = {
  fact: null,
  inference: '推測：',
  open_question: '待確認：',
}

/** 通過引用驗證的陳述，像一般文字那樣依序呈現；推測與待確認只加一個簡短前綴，來源收在下面的摺疊區。 */
function AnswerText({ statements }: { statements: QaStatement[] }) {
  return (
    <div className="qa-prose">
      {statements.map((statement) => {
        const prefix = KIND_PREFIX[statement.kind]
        return (
          <p key={statement.position}>
            {prefix !== null && <span className="muted">{prefix}</span>}
            {statement.text}
          </p>
        )
      })}
    </div>
  )
}

function uniqueCitations(statements: QaStatement[]): QaCitation[] {
  const seen = new Set<string>()
  const result: QaCitation[] = []
  for (const statement of statements) {
    for (const citation of statement.citations) {
      const key = `${citation.snapshot_id}:${citation.path}:${citation.start_line}:${citation.end_line}`
      if (seen.has(key)) continue
      seen.add(key)
      result.push(citation)
    }
  }
  return result
}

function Sources({ statements, presence }: { statements: QaStatement[]; presence: PresenceMap }) {
  const citations = uniqueCitations(statements)
  if (citations.length === 0) return null
  return (
    <details className="qa-sources">
      <summary>參考來源（{citations.length} 處）</summary>
      <ul className="qa-citations">
        {citations.map((citation) => (
          <li key={`${citation.snapshot_id}:${citation.path}:${citation.start_line}:${citation.end_line}`}>
            <QaSourceRef source={citation} presence={presence?.get(citation.snapshot_id)} />
          </li>
        ))}
      </ul>
    </details>
  )
}
function RejectedStatement({ item }: { item: QaRejectedStatement }) {
  return (
    <li className="qa-statement">
      <div>
        <span className={`badge ${QA_KIND_BADGE[item.kind]}`}>模型標為「{QA_KIND_LABEL[item.kind]}」</span>{' '}
        <span className="qa-statement-text">{item.text}</span>
      </div>
      <ul className="qa-reasons">
        {item.reasons.map((reason, index) => (
          <li key={index}>
            <strong>不採用的原因：</strong>
            {reason.citation_index !== null && `第 ${reason.citation_index + 1} 個引用：`}
            {rejectionLabel(reason.reason)}
          </li>
        ))}
      </ul>
      {item.claimed_citations.length > 0 && (
        <details>
          <summary>模型宣稱的引用（未驗證資料，共 {item.claimed_citations.length} 個）</summary>
          <ul className="qa-claimed">
            {item.claimed_citations.map((citation, index) => (
              <li key={index}>
                片段 <code>{citation.chunk}</code>・第 {citation.start_line}–{citation.end_line} 行・
                {citation.valid ? '這個引用本身有效' : `無效（${citation.reasons.map(rejectionLabel).join('；') || '原因未記錄'}）`}
              </li>
            ))}
          </ul>
        </details>
      )}
    </li>
  )
}

/**
 * 問答的答案：通過後端引用驗證的陳述直接當文字呈現；來源與沒通過驗證的陳述都收在摺疊區，不佔畫面。
 * 前端不判斷引用對不對、答案能不能用：一切依後端回傳的 statements／rejected_statements／answer_status。
 */
export function QaAnswer({ question, presence }: { question: QaQuestion; presence: PresenceMap }) {
  const verified = question.statements
  const rejected = question.rejected_statements
  const insufficient = question.answer_status === 'insufficient_evidence'
  return (
    <div className="qa-answer">
      {insufficient && verified.length > 0 && <p className="muted">現有的資料不足以完整回答，以下是說明：</p>}
      {verified.length === 0 && (
        <p className="warning" role="status">
          {insufficient
            ? '在選取的版本裡找不到足以回答的內容。可以換個問法，或選擇其他版本再試。'
            : '模型的回答沒有通過系統的來源驗證，所以沒有可用的答案。'}
        </p>
      )}
      <AnswerText statements={verified} />
      <Sources statements={verified} presence={presence} />
      {rejected.length > 0 && (
        <details className="qa-rejected">
          <summary>未採用的內容（{rejected.length} 項，沒通過來源驗證）</summary>
          <p className="note">這些不屬於答案，只供除錯參考；請不要當成事實。</p>
          <ol className="qa-statements">
            {rejected.map((item) => (
              <RejectedStatement key={item.position} item={item} />
            ))}
          </ol>
        </details>
      )}
    </div>
  )
}
