import {
  QA_KIND_BADGE, QA_KIND_LABEL, QA_KIND_NOTE, QA_KIND_ORDER, rejectionLabel,
} from '../qaQuestion'
import type { QaQuestion, QaRejectedStatement, QaStatement } from '../types'
import type { SnapshotPresence } from '../useSnapshotPresence'
import { QaSourceRef } from './QaSourceRef'

type PresenceMap = ReadonlyMap<number, SnapshotPresence> | null

function Statement({ statement, presence }: { statement: QaStatement; presence: PresenceMap }) {
  return (
    <li className="qa-statement">
      <div>
        <span className={`badge ${QA_KIND_BADGE[statement.kind]}`}>{QA_KIND_LABEL[statement.kind]}</span>{' '}
        <span className="qa-statement-text">{statement.text}</span>
      </div>
      {statement.citations.length === 0 ? (
        <p className="note">（沒有引用）</p>
      ) : (
        <ul className="qa-citations" aria-label={`第 ${statement.position + 1} 項陳述的引用`}>
          {statement.citations.map((citation, index) => (
            <li key={index}>
              <QaSourceRef source={citation} code={citation.chunk_code} presence={presence?.get(citation.snapshot_id)} />
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

/** 通過引用驗證的答案：依種類分組（事實、推測、待確認），每項都帶完整的來源位置。 */
function VerifiedStatements({ statements, presence }: { statements: QaStatement[]; presence: PresenceMap }) {
  return (
    <>
      {QA_KIND_ORDER.map((kind) => {
        const items = statements.filter((item) => item.kind === kind)
        if (items.length === 0) return null
        return (
          <section key={kind} className="qa-kind-group" aria-label={QA_KIND_LABEL[kind]}>
            <h4>
              <span className={`badge ${QA_KIND_BADGE[kind]}`}>{QA_KIND_LABEL[kind]}</span>
              <span className="muted qa-kind-note">（{items.length} 項）{QA_KIND_NOTE[kind]}</span>
            </h4>
            <ol className="qa-statements">
              {items.map((item) => (
                <Statement key={item.position} statement={item} presence={presence} />
              ))}
            </ol>
          </section>
        )
      })}
    </>
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
 * 問答的答案。只有通過後端引用驗證的陳述會出現在「答案」；沒通過的放在獨立的「引用無效」區，不混在答案裡。
 * 前端不判斷引用對不對、答案能不能用：一切依後端回傳的 statements／rejected_statements／answer_status。
 */
export function QaAnswer({ question, presence }: { question: QaQuestion; presence: PresenceMap }) {
  const verified = question.statements
  const rejected = question.rejected_statements
  const insufficient = question.answer_status === 'insufficient_evidence'
  return (
    <div className="qa-answer">
      <h3>答案</h3>
      {insufficient && (
        <div className="warning" role="status">
          <strong>找不到足夠證據</strong>
          <p>模型判斷送出的程式片段不足以回答這個問題，所以沒有提供答案。可以換個問法、選更多相關的版本，或確認檢索索引涵蓋了相關檔案。</p>
        </div>
      )}
      {!insufficient && verified.length === 0 && (
        <p className="warning" role="status">
          沒有任何陳述通過引用驗證，所以沒有可用的答案{rejected.length > 0 && '（見下方「引用無效」）'}。
        </p>
      )}
      <VerifiedStatements statements={verified} presence={presence} />
      {rejected.length > 0 && (
        <section className="qa-rejected" aria-labelledby={`qa-rejected-${question.id}`}>
          <h4 id={`qa-rejected-${question.id}`}>引用無效（{rejected.length} 項，未採用）</h4>
          <p className="note">
            下面的陳述沒有通過系統的引用驗證，<strong>不屬於上方的答案</strong>，只供除錯參考；請不要當成事實。
          </p>
          <ol className="qa-statements">
            {rejected.map((item) => (
              <RejectedStatement key={item.position} item={item} />
            ))}
          </ol>
        </section>
      )}
    </div>
  )
}
