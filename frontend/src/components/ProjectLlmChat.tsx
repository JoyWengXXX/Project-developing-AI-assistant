import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type {
  ProjectConfig, ProjectConversationMessage, ProjectCreateWithLlmResult, ProjectLlmStatus, Repository,
} from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { ReferenceSnapshotSelect } from './ReferenceSnapshotSelect'

const ERROR_HINTS = {
  project_answer_invalid: '回答不在這一題的選項內或格式不符；請從選項選擇或重新輸入。',
  snapshot_not_found: '找不到這個參考版本。',
  snapshot_not_completed: '這個參考版本尚未匯入完成。',
}

interface Answered {
  question: string
  answer: string
}

const LLM_NOTE: Record<string, string> = {
  qa_provider_not_configured: '尚未設定問答的 API key，沒有 LLM 推薦。',
  qa_model_not_selected: '尚未選擇模型，沒有 LLM 推薦。',
  qa_egress_not_consented: '參考 repo 尚未同意資料外送，沒有 LLM 推薦（仍有依參考版本分析的推薦）。',
  token_budget_exceeded: '額度不足，沒有 LLM 推薦。',
}

function llmNote(llm: ProjectLlmStatus): string | null {
  if (llm.state === 'skipped') return LLM_NOTE[llm.code ?? ''] ?? '沒有 LLM 推薦。'
  if (llm.state === 'failed') return `LLM 呼叫未成功（${llm.code ?? 'unknown'}），沒有 LLM 推薦。`
  return null
}

function toConversation(history: Answered[]): ProjectConversationMessage[] {
  return history.flatMap((item) => [
    { role: 'assistant' as const, content: item.question },
    { role: 'user' as const, content: item.answer },
  ])
}

/**
 * 「沒有初始骨架」的對話：後端依序問語言、框架、資料庫、ORM、測試框架。LLM 只提供推薦，
 * 決定權在使用者；對話狀態由前端保存（後端無狀態），每次把完整對話送回去。不會建立任何檔案。
 */
export function ProjectLlmChat({
  repos, snapshotKey, onCompleted,
}: {
  repos: Repository[]
  snapshotKey: number
  onCompleted?: (config: ProjectConfig) => void
}) {
  const [snapshotId, setSnapshotId] = useState<number | null>(null)
  const [history, setHistory] = useState<Answered[]>([])
  const [turn, setTurn] = useState<ProjectCreateWithLlmResult | null>(null)
  const [draft, setDraft] = useState('')
  const action = useAction()

  async function send(next: Answered[], referenceId: number | null) {
    const ok = await action.run(async () => {
      const body: { conversation: ProjectConversationMessage[]; reference_snapshot_id?: number } = {
        conversation: toConversation(next),
      }
      if (referenceId !== null) body.reference_snapshot_id = referenceId
      const response = await api<ProjectCreateWithLlmResult>('POST', '/api/v1/projects/create-with-llm', {
        body,
      })
      setTurn(response.data)
      if (response.data.done && response.data.project_config !== null) {
        onCompleted?.(response.data.project_config)
      }
    })
    if (ok) {
      setHistory(next)
      setDraft('')
    }
  }

  function answer(value: string) {
    if (turn === null || turn.question === null) return
    void send([...history, { question: turn.question, answer: value }], snapshotId)
  }

  function submitDraft(e: FormEvent) {
    e.preventDefault()
    if (draft.trim() !== '') answer(draft.trim())
  }

  function restart() {
    setHistory([])
    setTurn(null)
    setDraft('')
    action.clear()
  }

  const note = turn === null ? null : llmNote(turn.llm)
  return (
    <div>
      <ApiHint apis={['POST /api/v1/projects/create-with-llm（無狀態；LLM 只推薦，不建立檔案）']} />
      {turn === null && (
        <>
          <ReferenceSnapshotSelect
            repos={repos}
            snapshotId={snapshotId}
            onChange={setSnapshotId}
            snapshotKey={snapshotKey}
          />
          <p className="note">
            有選參考版本時，系統會分析它的語言與依賴（送給 LLM 的只有語言分布與依賴名稱，且該 repo 須已同意資料外送）。
          </p>
          <button type="button" disabled={action.busy} onClick={() => void send([], snapshotId)}>
            {action.busy ? '準備中…' : '開始對話'}
          </button>
        </>
      )}

      {history.length > 0 && (
        <ol className="chat-history" aria-label="已回答的問題">
          {history.map((item) => (
            <li key={item.question}>
              {item.question} → <strong>{item.answer}</strong>
            </li>
          ))}
        </ol>
      )}

      {turn !== null && !turn.done && turn.question !== null && (
        <div role="group" aria-label="目前的問題">
          <p>
            <strong>{turn.question}</strong>
          </p>
          {turn.recommendation !== null && (
            <p className="note">
              推薦（{turn.recommendation.source === 'llm' ? 'LLM' : '依參考版本分析'}）：
              <strong>{turn.recommendation.value}</strong>
              {turn.recommendation.reason !== null && <>――{turn.recommendation.reason}</>}{' '}
              <button type="button" disabled={action.busy} onClick={() => answer(turn.recommendation!.value)}>
                採用推薦
              </button>
            </p>
          )}
          {note !== null && <p className="muted">{note}</p>}
          <div>
            {turn.options.map((option) => (
              <button key={option} type="button" disabled={action.busy} onClick={() => answer(option)}>
                {option}
              </button>
            ))}
          </div>
          <form onSubmit={submitDraft} className="form">
            <label>
              或自由輸入
              <input type="text" value={draft} maxLength={60} onChange={(e) => setDraft(e.target.value)} />
            </label>
            <button type="submit" disabled={action.busy || draft.trim() === ''}>
              送出
            </button>
          </form>
        </div>
      )}

      {turn !== null && turn.done && turn.project_config !== null && (
        <div className="success" role="status">
          <p>
            配置完成：<strong>{turn.project_config.language}</strong> / {turn.project_config.framework ?? '（無框架）'}
          </p>
          <ul>
            <li>資料庫：{turn.project_config.database ?? '—'}</li>
            <li>ORM：{turn.project_config.orm ?? '—'}</li>
            <li>測試框架：{turn.project_config.test_framework ?? '—'}</li>
          </ul>
          <details>
            <summary>依賴（{Object.keys(turn.project_config.dependencies).length} 項）</summary>
            <ul>
              {Object.entries(turn.project_config.dependencies).map(([name, spec]) => (
                <li key={name}>
                  {name} {spec}
                </li>
              ))}
            </ul>
          </details>
          {turn.template !== null ? (
            <p>
              內建範本：<code>{turn.template.name}</code>（{turn.template.files.length} 個檔案，位於{' '}
              <code>{turn.template.template_path}</code>）。系統不會自動複製或安裝；請自行複製範本後再用「匯入初始專案」驗證。
            </p>
          ) : (
            <p className="note">這個框架沒有內建範本；請依框架官方文件自行初始化。</p>
          )}
        </div>
      )}

      <ErrorBox error={action.error} hints={ERROR_HINTS} />
      {turn !== null && (
        <p>
          <button type="button" onClick={restart} disabled={action.busy}>
            重新開始
          </button>
        </p>
      )}
    </div>
  )
}
