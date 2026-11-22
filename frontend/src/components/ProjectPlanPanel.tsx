import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { ProjectConfig, ProjectPlan, Repository } from '../types'
import { ApiHint } from '../ui'
import { useAction } from '../useAction'
import { QaErrorBox } from './QaErrorBox'
import { ReferenceSnapshotSelect } from './ReferenceSnapshotSelect'

const MAX_REQUIREMENTS = 4000

const COMPLEXITY_TEXT = { low: '低', medium: '中', high: '高' }

function PlanView({ plan }: { plan: ProjectPlan }) {
  const doc = plan.plan
  return (
    <div>
      <p>
        <span className={plan.locked ? 'badge badge-completed' : 'badge badge-neutral'}>
          {plan.locked ? '已確認（鎖定）' : '草稿'}
        </span>{' '}
        計畫 #{plan.id}・參考 {plan.reference.repository_name}@<code>{plan.reference.commit_sha.slice(0, 12)}</code>
        {!plan.reference.available && <span className="warning">（參考快照已被移除，此計畫不應再作為依據）</span>}
      </p>
      <p>
        用量：輸入 {plan.usage.input_tokens.toLocaleString()}、輸出 {plan.usage.output_tokens.toLocaleString()} token・
        USD {plan.usage.cost_usd}（{plan.usage.model}）{!plan.usage.usage_known && '（供應商未回報用量，以預留量計）'}
      </p>
      <h4>推薦架構（模型推測）</h4>
      <p>{doc.architecture.summary}</p>
      <p className="muted">
        參考 repo：{doc.reference_analysis.detected !== null && <>事實（系統偵測）{doc.reference_analysis.detected.language} / {doc.reference_analysis.detected.framework ?? '未偵測到框架'}；</>}
        推測 架構模式 {doc.reference_analysis.architecture_pattern}、層級 {doc.reference_analysis.layer_count ?? '未知'}、核心模組{' '}
        {doc.reference_analysis.core_modules.join('、') || '未識別'}
      </p>
      <h4>預期檔案清單（{doc.files.length} 個）</h4>
      <table className="table">
        <thead>
          <tr>
            <th>檔案</th>
            <th>模組</th>
            <th>用途</th>
            <th>複雜度</th>
            <th>參考來源</th>
          </tr>
        </thead>
        <tbody>
          {doc.files.map((file) => (
            <tr key={file.path}>
              <td>
                <code>{file.path}</code>
              </td>
              <td>{file.module}</td>
              <td>{file.purpose}</td>
              <td>{COMPLEXITY_TEXT[file.complexity]}</td>
              <td>
                {file.references.length === 0 && <span className="muted">無（沒有可驗證的參考）</span>}
                {file.references.map((ref) => (
                  <div key={`${ref.path}:${ref.start_line}-${ref.end_line}`}>
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
      <p>
        工作量（粗估）：低 {doc.workload.by_complexity.low}／中 {doc.workload.by_complexity.medium}／高{' '}
        {doc.workload.by_complexity.high}；生成時約 {doc.workload.input_tokens.toLocaleString()} 輸入、
        {doc.workload.output_tokens.toLocaleString()} 輸出 token，約 USD {doc.workload.cost_usd}。這是固定假設，不是實測或報價。
      </p>
      {(doc.rejected_files.length > 0 || doc.dropped_references.length > 0 || doc.warnings.length > 0) && (
        <details>
          <summary>
            驗證記錄（拒絕 {doc.rejected_files.length} 個檔案、丟棄 {doc.dropped_references.length} 個參考位置）
          </summary>
          <ul>
            {doc.rejected_files.map((item, index) => (
              <li key={`r${index}`}>
                檔案 <code>{item.path ?? '（格式不符）'}</code>：{item.reason}
              </li>
            ))}
            {doc.dropped_references.map((item, index) => (
              <li key={`d${index}`}>
                參考 <code>{item.path}:{item.start_line}-{item.end_line}</code>（{item.file}）：{item.reason}
              </li>
            ))}
            {doc.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </details>
      )}
      {doc.open_questions.length > 0 && (
        <>
          <h4>待確認事項</h4>
          <ul>
            {doc.open_questions.map((question) => (
              <li key={question}>{question}</li>
            ))}
          </ul>
        </>
      )}
      <details>
        <summary>計畫文件（Markdown 原文）</summary>
        <pre className="plan-markdown">{plan.plan_markdown}</pre>
      </details>
    </div>
  )
}

/**
 * 「規劃新專案」：依已確認的初始骨架配置（#18／#19 的結果）與一個參考版本，由模型規劃檔案清單。
 * 規劃是一次同步的模型呼叫（可能要數十秒，會計入問答額度）；不會寫入任何檔案。
 * 確認後計畫鎖定；要改需求就重新規劃（產生新的草稿，已確認的計畫不變）。
 */
export function ProjectPlanPanel({
  config, repos, snapshotKey,
}: {
  config: ProjectConfig
  repos: Repository[]
  snapshotKey: number
}) {
  const [snapshotId, setSnapshotId] = useState<number | null>(null)
  const [requirements, setRequirements] = useState('')
  const [plan, setPlan] = useState<ProjectPlan | null>(null)
  const planning = useAction()
  const confirming = useAction()

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (snapshotId === null) return
    confirming.clear()
    await planning.run(async () => {
      const response = await api<ProjectPlan>('POST', '/api/v1/projects/plan', {
        body: {
          target_project_config: config,
          reference_snapshot_id: snapshotId,
          project_requirements: requirements,
        },
      })
      setPlan(response.data)
    })
  }

  async function confirm() {
    if (plan === null) return
    await confirming.run(async () => {
      const response = await api<ProjectPlan>('POST', `/api/v1/projects/plans/${plan.id}/confirm`)
      setPlan(response.data)
    })
  }

  const locked = plan?.locked === true
  return (
    <div>
      <ApiHint
        apis={[
          'POST /api/v1/projects/plan（同步呼叫模型，201 回傳草稿計畫）',
          'POST /api/v1/projects/plans/{id}/confirm（確認並鎖定，冪等）',
        ]}
      />
      <p className="note">
        目標配置：<strong>{config.language}</strong> / {config.framework ?? '（無框架）'}
        {config.database && <>・{config.database}</>}
        {config.orm && <>・{config.orm}</>}
        {config.test_framework && <>・{config.test_framework}</>}
      </p>
      <form onSubmit={submit} className="form">
        <ReferenceSnapshotSelect
          repos={repos}
          snapshotId={snapshotId}
          onChange={setSnapshotId}
          snapshotKey={snapshotKey}
        />
        <label>
          專案需求
          <textarea
            value={requirements}
            maxLength={MAX_REQUIREMENTS}
            rows={5}
            onChange={(e) => setRequirements(e.target.value)}
            placeholder="例：內部報表系統，支援 SSO 登入與報表匯出"
            required
          />
        </label>
        <p className="note">
          送給模型的只有參考版本的語言、依賴、目錄、檔案路徑與符號位置（不含原始碼內容），以及上面的需求；該參考 repo 須已同意資料外送。
        </p>
        <button type="submit" disabled={planning.busy || snapshotId === null || requirements.trim() === ''}>
          {planning.busy ? '規劃中（可能需要數十秒）…' : plan === null ? '產生計畫' : '修改需求後重新規劃'}
        </button>
      </form>
      <QaErrorBox error={planning.error} title="規劃失敗" />
      {plan !== null && (
        <>
          <PlanView plan={plan} />
          <p>
            <button type="button" onClick={() => void confirm()} disabled={confirming.busy || locked}>
              {locked ? '計畫已確認' : confirming.busy ? '確認中…' : '確認計畫'}
            </button>
          </p>
          {locked && (
            <p className="success" role="status">
              計畫已鎖定，將作為第七階段（逐檔生成）的唯一輸入；要調整需求請重新規劃（會產生新的草稿，這份不會被改動）。
            </p>
          )}
          <QaErrorBox error={confirming.error} title="確認失敗" />
        </>
      )}
    </div>
  )
}
