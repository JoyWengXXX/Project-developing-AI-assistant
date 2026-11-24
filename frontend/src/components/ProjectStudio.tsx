import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { api } from '../api'
import { navigateTo } from '../navigation'
import type {
  ProjectConfig, ProjectConversationMessage, ProjectCreateWithLlmResult, ProjectLlmStatus, ProjectPlan, Repository,
} from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import type { QaSettingsState } from '../useQaSettings'
import type { UiMode } from '../useUiMode'
import { NARROW_COLS, WIDE_COLS, zoneOf, type SheetModule, type StackRow } from '../blueprintLayout'
import { BlueprintSheet } from './BlueprintSheet'
import { ProjectInitForm } from './ProjectInitForm'
import { ProjectPlanList } from './ProjectPlanList'
import { PlanView } from './ProjectPlanPanel'
import { QaErrorBox } from './QaErrorBox'
import { ReferenceSnapshotSelect } from './ReferenceSnapshotSelect'

const MAX_REQUIREMENTS = 4000

const CHAT_HINTS = {
  project_answer_invalid: '這一題不接受這個回答（空白、含 < > { } ` \\ 等字元、超過 60 字）。請改用選項按鈕，或換個說法。',
  snapshot_not_found: '找不到這個參考版本，請在「參考專案」重新選擇。',
  snapshot_not_completed: '這個參考版本還沒匯入完成，請稍後再試。',
}

const LLM_NOTE: Record<string, string> = {
  qa_provider_not_configured: '還沒設定 API key，這題沒有 AI 推薦。',
  qa_model_not_selected: '還沒選擇模型，這題沒有 AI 推薦。',
  qa_egress_not_consented: '參考專案還沒同意資料外送，這題只有依參考專案分析的推薦。',
  token_budget_exceeded: '今日額度不足，這題沒有 AI 推薦。',
}

function llmNote(llm: ProjectLlmStatus): string | null {
  if (llm.state === 'skipped') return LLM_NOTE[llm.code ?? ''] ?? '這題沒有 AI 推薦。'
  if (llm.state === 'failed') return 'AI 推薦這次沒有成功，仍可自己選擇。'
  return null
}

const STACK_FIELDS: { key: keyof ProjectConfig; label: string }[] = [
  { key: 'language', label: '語言' },
  { key: 'framework', label: '框架' },
  { key: 'database', label: '資料庫' },
  { key: 'orm', label: '資料存取' },
  { key: 'test_framework', label: '測試' },
]

const COMPLEXITY_TEXT = { low: '低', medium: '中', high: '高' }

/** 後端金額是 8 位小數字串；顯示時保留 2 位，小於 0.01 的保留 4 位。 */
function usd(value: string): string {
  const n = Number(value)
  if (!Number.isFinite(n)) return `USD ${value}`
  return `USD ${n.toFixed(n !== 0 && Math.abs(n) < 0.01 ? 4 : 2)}`
}

type Phase = 'describe' | 'stack' | 'review' | 'blueprint'

interface Answered {
  question: string
  answer: string
}

function toConversation(history: Answered[]): ProjectConversationMessage[] {
  return history.flatMap((item) => [
    { role: 'assistant' as const, content: item.question },
    { role: 'user' as const, content: item.answer },
  ])
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path d="M3 8.5 L6.5 12 L13 4.5" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

const STEPS = ['描述需求', '選擇技術', '看藍圖', '確認計畫', '產生專案']

const NARROW_QUERY = '(max-width: 720px)'

/** 窄螢幕時藍圖改成每列 2 個模組，縮放後字仍看得清楚。 */
function useNarrow(): boolean {
  const [narrow, setNarrow] = useState(() => window.matchMedia(NARROW_QUERY).matches)
  useEffect(() => {
    const query = window.matchMedia(NARROW_QUERY)
    const onChange = () => setNarrow(query.matches)
    query.addEventListener('change', onChange)
    return () => query.removeEventListener('change', onChange)
  }, [])
  return narrow
}

function Journey({ current, locked }: { current: number; locked: boolean }) {
  return (
    <ol className="journey" aria-label="建立新專案的步驟">
      {STEPS.map((label, index) => {
        const step = index + 1
        let state = step < current ? 'done' : step === current ? 'current' : 'todo'
        if (step === 4 && locked) state = 'done'
        if (step === 5) state = 'closed'
        return (
          <li key={label} data-state={state} aria-current={state === 'current' ? 'step' : undefined}>
            <span className="journey-mark">{state === 'done' ? <CheckIcon /> : step}</span>
            <span className="journey-label">
              {label}
              {step === 5 && <span className="journey-note">尚未開放</span>}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

/**
 * 新專案工作室：描述需求 → 對話選技術（LLM 只推薦，決定權在使用者）→ 送出前確認 → 藍圖 → 確認並鎖定。
 * 只呼叫既有 API：POST /projects/create-with-llm（無狀態，每次送完整對話）、POST /projects/plan、
 * POST /projects/plans/{id}/confirm。對話與需求只存在記憶體，不寫入瀏覽器儲存。
 */
export function ProjectStudio({
  repos, snapshotKey, settings, mode,
}: {
  repos: Repository[]
  snapshotKey: number
  settings: QaSettingsState
  mode: UiMode
}) {
  const [phase, setPhase] = useState<Phase>('describe')
  const [requirements, setRequirements] = useState('')
  const [snapshotId, setSnapshotId] = useState<number | null>(null)
  const [snapshotLabel, setSnapshotLabel] = useState<string | null>(null)
  const [history, setHistory] = useState<Answered[]>([])
  const [turn, setTurn] = useState<ProjectCreateWithLlmResult | null>(null)
  const [config, setConfig] = useState<ProjectConfig | null>(null)
  const [draft, setDraft] = useState('')
  const [changedLabel, setChangedLabel] = useState<string | null>(null)
  const [plan, setPlan] = useState<ProjectPlan | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [confirmOpen, setConfirmOpen] = useState(false)
  const [plansKey, setPlansKey] = useState(0)
  const chat = useAction()
  const planning = useAction()
  const confirming = useAction()
  const qa = settings.settings
  const narrow = useNarrow()
  const cols = narrow ? NARROW_COLS : WIDE_COLS

  const stack: StackRow[] = STACK_FIELDS.map(({ key, label }) => {
    const value = config !== null ? config[key] : (turn?.collected[key] ?? null)
    return { label, value: typeof value === 'string' ? value : config !== null ? '不使用' : null }
  })

  const modules: SheetModule[] = useMemo(() => {
    if (plan === null) return []
    const doc = plan.plan
    const refs = new Map<string, string[]>()
    doc.files.forEach((f) =>
      refs.set(f.module, [...(refs.get(f.module) ?? []), ...f.references.map((r) => `${r.path}:${r.start_line}-${r.end_line}`)]),
    )
    const list: SheetModule[] = doc.architecture.modules.map((m) => ({
      name: m.name, purpose: m.purpose, dependsOn: m.depends_on, refs: refs.get(m.name) ?? [],
    }))
    const known = new Set(list.map((m) => m.name))
    doc.files.forEach((f) => {
      if (known.has(f.module)) return
      known.add(f.module)
      list.push({ name: f.module, purpose: '（計畫未另外說明）', dependsOn: [], refs: refs.get(f.module) ?? [] })
    })
    return list
  }, [plan])

  async function ask(next: Answered[]) {
    const previous = turn?.collected ?? {}
    const ok = await chat.run(async () => {
      const body: { conversation: ProjectConversationMessage[]; reference_snapshot_id?: number; project_requirements?: string } = {
        conversation: toConversation(next),
      }
      if (requirements.trim() !== '') body.project_requirements = requirements.trim()
      if (snapshotId !== null) body.reference_snapshot_id = snapshotId
      const response = await api<ProjectCreateWithLlmResult>('POST', '/api/v1/projects/create-with-llm', { body })
      const result = response.data
      setTurn(result)
      const changed = STACK_FIELDS.find(({ key }) => (result.collected[key] ?? null) !== (previous[key] ?? null))
      setChangedLabel(changed?.label ?? null)
      setConfig(result.done ? result.project_config : null)
    })
    if (ok) {
      setHistory(next)
      setDraft('')
    }
  }

  function startChat() {
    setPhase('stack')
    setHistory([])
    setTurn(null)
    setConfig(null)
    void ask([])
  }

  function answer(value: string) {
    if (turn === null || turn.question === null) return
    void ask([...history, { question: turn.question, answer: value }])
  }

  function submitDraft(e: FormEvent) {
    e.preventDefault()
    if (draft.trim() !== '') answer(draft.trim())
  }

  async function generate() {
    if (config === null) return
    confirming.clear()
    const ok = await planning.run(async () => {
      const body: Record<string, unknown> = { target_project_config: config, project_requirements: requirements }
      if (snapshotId !== null) body.reference_snapshot_id = snapshotId
      const response = await api<ProjectPlan>('POST', '/api/v1/projects/plan', { body })
      setPlan(response.data)
      const doc = response.data.plan
      const first = doc.files.find((f) => f.references.length > 0)?.module ?? doc.architecture.modules[0]?.name ?? null
      setSelected(first)
      setPlansKey((k) => k + 1)
    })
    settings.reload() // 規劃會計入今日用量
    if (ok) {
      setPhase('blueprint')
      setConfirmOpen(false)
    }
  }

  async function lock() {
    if (plan === null) return
    const ok = await confirming.run(async () => {
      const response = await api<ProjectPlan>('POST', `/api/v1/projects/plans/${plan.id}/confirm`)
      setPlan(response.data)
      setPlansKey((k) => k + 1)
    })
    if (ok) setConfirmOpen(false)
  }

  const locked = plan?.locked === true
  const current = phase === 'describe' ? 1 : phase === 'stack' ? 2 : plan === null ? 3 : locked ? 5 : 4
  const recommended = turn?.recommendation?.value ?? null
  const options = turn === null ? [] : turn.options
  const note = turn === null ? null : llmNote(turn.llm)
  const selectedModule = modules.find((m) => m.name === selected) ?? null
  const selectedFiles = plan === null || selected === null ? [] : plan.plan.files.filter((f) => f.module === selected)

  return (
    <section className="studio" aria-labelledby="studio-title">
      <div className="studio-head">
        <h1 id="studio-title" className="sr-only">
          建立新專案
        </h1>
        <Journey current={current} locked={locked} />
      </div>

      {qa !== null && !qa.can_ask && (
        <div className="studio-prereq" role="status">
          <p>
            AI 推薦與產生藍圖需要先{qa.model === null ? '選擇模型並' : ''}設定 API key。沒設定也能用選項完成技術選擇。
          </p>
          <button type="button" className="secondary small" onClick={() => navigateTo('settings')}>
            前往設定
          </button>
        </div>
      )}

      <div className="studio-grid">
        <div className="studio-side">
          {phase === 'describe' && (
            <div className="panel studio-card">
              <h2>你想做什麼系統？</h2>
              <p className="muted">用自己的話描述就好：誰會用、要能做哪些事。</p>
              <label className="sr-only" htmlFor="studio-requirements">
                專案需求
              </label>
              <textarea
                id="studio-requirements"
                className="studio-textarea"
                value={requirements}
                maxLength={MAX_REQUIREMENTS}
                rows={6}
                onChange={(e) => setRequirements(e.target.value)}
                placeholder="例：社區活動報名系統，居民可以線上報名，管理員可以看名單並匯出 Excel。"
              />
              <p className="muted studio-count">
                {requirements.length} / {MAX_REQUIREMENTS} 字
              </p>
              <details className="studio-more">
                <summary>參考既有專案（選填）</summary>
                <ReferenceSnapshotSelect repos={repos} snapshotId={snapshotId} onChange={(id, label) => { setSnapshotId(id); setSnapshotLabel(label) }} snapshotKey={snapshotKey} />
                <p className="muted">
                  選了參考版本，系統會依它推薦技術，藍圖也會標出可參考的位置。送出的只有語言、依賴、目錄、檔案路徑與符號位置，不含原始碼；
                  該專案須先在「設定」同意資料外送。
                </p>
                {repos.length === 0 && (
                  <button type="button" className="secondary small" onClick={() => navigateTo('repos')}>
                    新增參考專案
                  </button>
                )}
              </details>
              {mode === 'developer' && (
                <details className="studio-more">
                  <summary>已有專案資料夾：偵測現有配置</summary>
                  <ProjectInitForm
                    onDetected={(result) => {
                      setConfig(result.project_config)
                      setTurn(null)
                      setHistory([])
                    }}
                  />
                </details>
              )}
              <div className="studio-actions">
                {config !== null ? (
                  <>
                    <button type="button" disabled={requirements.trim() === ''} onClick={() => setPhase('review')}>
                      下一步：檢查後產生藍圖
                    </button>
                    <button type="button" className="secondary" onClick={startChat}>
                      重新選擇技術
                    </button>
                  </>
                ) : (
                  <button type="button" disabled={requirements.trim() === ''} onClick={startChat}>
                    下一步：選擇技術
                  </button>
                )}
              </div>
              {requirements.trim() === '' && (
                <p className="muted" role="status">
                  先寫下需求，才能繼續。
                </p>
              )}
            </div>
          )}

          {phase === 'stack' && (
            <div className="panel studio-card chat">
              <div className="chat-head">
                <h2>選擇技術</h2>
                <span className="muted">AI 只推薦，由你決定</span>
              </div>
              <ol className="chat-log" aria-live="polite" aria-label="對話">
                <li className="bubble bubble-me">
                  <span className="bubble-who">你</span>
                  <p>{requirements}</p>
                </li>
                {history.map((item, index) => (
                  <li key={`${index}-${item.question}`} className="bubble-pair">
                    <div className="bubble bubble-ai">
                      <span className="bubble-who">RepoPilot</span>
                      <p>{item.question}</p>
                    </div>
                    <div className="bubble bubble-me">
                      <span className="bubble-who">你</span>
                      <p>{item.answer}</p>
                      <button
                        type="button"
                        className="linklike"
                        disabled={chat.busy}
                        onClick={() => {
                          setConfig(null)
                          void ask(history.slice(0, index))
                        }}
                      >
                        改這題
                      </button>
                    </div>
                  </li>
                ))}
                {chat.busy && (
                  <li className="bubble bubble-ai" role="status">
                    <span className="bubble-who">RepoPilot</span>
                    <p className="muted">思考中…</p>
                  </li>
                )}
                {!chat.busy && turn !== null && !turn.done && turn.question !== null && (
                  <li className="bubble bubble-ai bubble-current">
                    <span className="bubble-who">RepoPilot</span>
                    <p className="bubble-question">{turn.question}</p>
                    {turn.reference_note != null && <p className="warning">{turn.reference_note}</p>}
                    {turn.recommendation !== null && (
                      <p className="bubble-reason">
                        推薦 <strong>{turn.recommendation.value}</strong>
                        {turn.recommendation.reason !== null && <>：{turn.recommendation.reason}</>}
                        <span className="muted">（{turn.recommendation.source === 'llm' ? 'AI 推薦' : '依參考專案分析'}）</span>
                      </p>
                    )}
                    {note !== null && <p className="muted">{note}</p>}
                    <div className="chat-options" role="group" aria-label="選項">
                      {recommended !== null && !options.includes(recommended) && (
                        <button type="button" onClick={() => answer(recommended)}>
                          {recommended}（推薦）
                        </button>
                      )}
                      {options.map((option) => (
                        <button
                          key={option}
                          type="button"
                          className={option === recommended ? undefined : 'secondary'}
                          onClick={() => answer(option)}
                        >
                          {option}
                          {option === recommended && '（推薦）'}
                        </button>
                      ))}
                    </div>
                  </li>
                )}
                {!chat.busy && turn !== null && turn.done && config !== null && (
                  <li className="bubble bubble-ai">
                    <span className="bubble-who">RepoPilot</span>
                    <p>技術選好了，右邊的標題欄已經填上。下一步檢查要送出的內容，再產生藍圖。</p>
                    {turn.template !== null && (
                      <p className="muted">
                        系統有對應的起始範本「{turn.template.name}」（{turn.template.files.length} 個檔案）。目前不會自動建立檔案。
                      </p>
                    )}
                  </li>
                )}
              </ol>
              <ErrorBox error={chat.error} hints={CHAT_HINTS} />
              {turn !== null && !turn.done && turn.question !== null && (
                <form className="chat-input" onSubmit={submitDraft}>
                  <label className="sr-only" htmlFor="studio-answer">
                    用自己的話回答
                  </label>
                  <input
                    id="studio-answer"
                    type="text"
                    value={draft}
                    maxLength={60}
                    placeholder="或用自己的話回答…"
                    onChange={(e) => setDraft(e.target.value)}
                    disabled={chat.busy}
                  />
                  <button type="submit" className="secondary" disabled={chat.busy || draft.trim() === ''}>
                    送出
                  </button>
                </form>
              )}
              <div className="studio-actions">
                {config !== null && (
                  <button type="button" onClick={() => setPhase('review')}>
                    下一步：檢查後產生藍圖
                  </button>
                )}
                <button type="button" className="secondary" disabled={chat.busy} onClick={() => setPhase('describe')}>
                  回到需求
                </button>
                {chat.error !== null && turn === null && (
                  <button type="button" className="secondary" onClick={startChat}>
                    再試一次
                  </button>
                )}
              </div>
            </div>
          )}

          {phase === 'review' && config !== null && (
            <div className="panel studio-card">
              <h2>送出前確認</h2>
              <p className="muted">產生藍圖會呼叫 AI 模型一次，可能需要數十秒，並計入今日額度。</p>
              <dl className="review-list">
                <dt>需求描述</dt>
                <dd>{requirements.trim().length} 字</dd>
                <dt>技術</dt>
                <dd>{STACK_FIELDS.map(({ key }) => config[key]).filter((v) => typeof v === 'string').join(' · ')}</dd>
                <dt>參考專案</dt>
                <dd>
                  {snapshotId === null
                    ? '不使用：不送任何專案資料'
                    : `${snapshotLabel ?? '已選的參考版本'}：只送語言、依賴、目錄、檔案路徑與符號位置，不含原始碼`}
                </dd>
                <dt>模型</dt>
                <dd>{qa?.model ?? '尚未選擇'}</dd>
                <dt>今日剩餘</dt>
                <dd>{qa === null ? '讀取中' : qa.usage.remaining_cost_usd === null ? '未設每日上限' : usd(qa.usage.remaining_cost_usd)}</dd>
              </dl>
              {planning.busy && (
                <p role="status" className="studio-busy">
                  正在繪製藍圖（可能需要數十秒）…
                </p>
              )}
              <QaErrorBox error={planning.error} title="藍圖沒有產生" />
              <div className="studio-actions">
                <button type="button" disabled={planning.busy || requirements.trim() === ''} onClick={() => void generate()}>
                  {planning.busy ? '繪製中…' : '送出並產生藍圖'}
                </button>
                <button type="button" className="secondary" disabled={planning.busy} onClick={() => setPhase('describe')}>
                  返回修改
                </button>
              </div>
            </div>
          )}

          {phase === 'blueprint' && plan !== null && (
            <div className="panel studio-card">
              <h2>{locked ? '計畫已確認' : '藍圖草稿'}</h2>
              <p>{plan.plan.architecture.summary}</p>
              {plan.plan.open_questions.length > 0 && (
                <div className="studio-questions">
                  <h3>待確認 {plan.plan.open_questions.length} 項</h3>
                  <ul>
                    {plan.plan.open_questions.map((question) => (
                      <li key={question}>{question}</li>
                    ))}
                  </ul>
                </div>
              )}
              {plan.reference !== null && !plan.reference.available && (
                <p className="warning">這份藍圖參考的版本已被移除，參考位置可能無法再查證。</p>
              )}
              {!locked && !confirmOpen && (
                <div className="studio-actions">
                  <button type="button" onClick={() => setConfirmOpen(true)}>
                    確認計畫
                  </button>
                  <button type="button" className="secondary" onClick={() => setPhase('describe')}>
                    修改需求後重新規劃
                  </button>
                </div>
              )}
              {!locked && confirmOpen && (
                <div className="lock-box" role="group" aria-label="確認計畫">
                  <p>
                    確認後這份計畫會鎖定，之後「產生專案」會以它為唯一依據。要改內容只能重新規劃（產生新的草稿），這份不會被改動。
                  </p>
                  <div className="studio-actions">
                    <button type="button" disabled={confirming.busy} onClick={() => void lock()}>
                      {confirming.busy ? '確認中…' : '確認並鎖定'}
                    </button>
                    <button type="button" className="secondary" disabled={confirming.busy} onClick={() => setConfirmOpen(false)}>
                      先不要
                    </button>
                  </div>
                </div>
              )}
              <QaErrorBox error={confirming.error} title="沒有確認成功" />
              {locked && (
                <div className="success" role="status">
                  <p>計畫已鎖定。「產生專案」尚未開放；要調整請修改需求後重新規劃。</p>
                  <button type="button" className="secondary small" onClick={() => setPhase('describe')}>
                    修改需求後重新規劃
                  </button>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="studio-main">
          <div className="sheet-wrap">
            <BlueprintSheet
              title={requirements.trim() === '' ? '新專案' : requirements.trim()}
              sheetNo={plan === null ? '未編號' : `P-${plan.id}`}
              status={plan === null ? '未產生' : locked ? '已確認' : '草稿'}
              stack={stack}
              modules={modules}
              refSource={
                plan?.reference != null ? `${plan.reference.repository_name} @ ${plan.reference.commit_sha.slice(0, 7)}` : null
              }
              cols={cols}
              openQuestions={plan?.plan.open_questions.length ?? 0}
              selected={selected}
              onSelect={setSelected}
              changedLabel={changedLabel}
              emptyText={phase === 'review' || planning.busy ? '送出後，模組會畫在這裡' : '選好技術並產生藍圖後，模組會畫在這裡'}
            />
          </div>

          {plan !== null && selectedModule !== null && (
            <div className="panel module-detail" aria-live="polite">
              <div className="module-detail-head">
                <h2>
                  <span className="module-zone">{zoneOf(modules, selectedModule.name, cols)}</span>
                  {selectedModule.name}
                </h2>
                <span className="muted">
                  {selectedFiles.length} 個檔案
                  {selectedModule.dependsOn.length > 0 && `・依賴 ${selectedModule.dependsOn.join('、')}`}
                </span>
              </div>
              <p>{selectedModule.purpose}</p>
              {selectedFiles.length > 0 && (
                <div className="table-scroll">
                  <table className="table">
                    <thead>
                      <tr>
                        <th scope="col">檔案</th>
                        <th scope="col">用途</th>
                        <th scope="col">工作量</th>
                        <th scope="col">參考位置</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedFiles.map((file) => (
                        <tr key={file.path}>
                          <td>
                            <code>{file.path}</code>
                          </td>
                          <td>{file.purpose}</td>
                          <td>{COMPLEXITY_TEXT[file.complexity]}</td>
                          <td>
                            {file.references.length === 0 && <span className="muted">無</span>}
                            {file.references.map((ref) => (
                              <div key={`${ref.path}:${ref.start_line}`} className="ref-line">
                                {plan.reference !== null && (
                                  <span className="muted">
                                    {plan.reference.repository_name} @ {plan.reference.commit_sha.slice(0, 7)} ·{' '}
                                  </span>
                                )}
                                <code>
                                  {ref.path}:{ref.start_line}-{ref.end_line}
                                </code>
                              </div>
                            ))}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <CostStrip settings={settings} plan={plan} />

          {plan !== null && (
            <details className="panel advanced">
              <summary>完整計畫內容（檔案清單、驗證記錄、Markdown）</summary>
              <PlanView plan={plan} />
            </details>
          )}
        </div>
      </div>

      <details className="panel advanced">
        <summary>已保存的計畫</summary>
        <ProjectPlanList
          reloadKey={plansKey}
          onDeleted={(id) => {
            if (plan?.id === id) {
              setPlan(null)
              setSelected(null)
              if (phase === 'blueprint') setPhase('review')
            }
            setPlansKey((k) => k + 1)
          }}
        />
      </details>
    </section>
  )
}

function CostStrip({ settings, plan }: { settings: QaSettingsState; plan: ProjectPlan | null }) {
  const qa = settings.settings
  if (qa === null) return null
  const used = Number(qa.usage.today.cost_usd)
  const limit = qa.limits.daily_cost_usd === null ? null : Number(qa.limits.daily_cost_usd)
  const ratio = limit === null || limit <= 0 ? null : Math.min(1, used / limit)
  return (
    <div className="panel cost-strip">
      <div className="cost-item">
        <span className="cost-label">今日已用</span>
        <span className="cost-value">{usd(qa.usage.today.cost_usd)}</span>
      </div>
      <div className="cost-meter">
        {ratio !== null ? (
          <>
            <div
              className="cost-bar"
              role="meter"
              aria-label="今日額度使用"
              aria-valuemin={0}
              aria-valuemax={limit ?? 0}
              aria-valuenow={used}
              aria-valuetext={`已用 ${usd(qa.usage.today.cost_usd)}，上限 ${usd(qa.limits.daily_cost_usd ?? '0')}`}
            >
              <span style={{ width: `${Math.max(ratio * 100, 1)}%` }} />
            </div>
            <span className="cost-label">每日上限 {usd(qa.limits.daily_cost_usd ?? '0')}</span>
          </>
        ) : (
          <span className="cost-label">未設每日上限（可在「設定」設定）</span>
        )}
      </div>
      {plan !== null && (
        <>
          <div className="cost-item">
            <span className="cost-label">這份藍圖花費</span>
            <span className="cost-value">{usd(plan.usage.cost_usd)}</span>
          </div>
          <div className="cost-item">
            <span className="cost-label">產生專案粗估（不是報價）</span>
            <span className="cost-value">{usd(plan.plan.workload.cost_usd)}</span>
          </div>
        </>
      )}
    </div>
  )
}
