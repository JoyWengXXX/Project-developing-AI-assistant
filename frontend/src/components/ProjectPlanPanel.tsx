import type { ProjectPlan } from '../types'

const COMPLEXITY_TEXT = { low: '低', medium: '中', high: '高' }

export function PlanView({ plan }: { plan: ProjectPlan }) {
  const doc = plan.plan
  return (
    <div>
      <p>
        <span className={plan.locked ? 'badge badge-completed' : 'badge badge-neutral'}>
          {plan.locked ? '已確認（鎖定）' : '草稿'}
        </span>{' '}
        計畫 #{plan.id}・
        {plan.reference === null ? (
          <>未使用參考 repo（僅依需求與配置）</>
        ) : (
          <>
            參考 {plan.reference.repository_name}@<code>{plan.reference.commit_sha.slice(0, 12)}</code>
            {!plan.reference.available && <span className="warning">（參考快照已被移除，此計畫不應再作為依據）</span>}
          </>
        )}
      </p>
      <p>
        用量：輸入 {plan.usage.input_tokens.toLocaleString()}、輸出 {plan.usage.output_tokens.toLocaleString()} token・
        USD {plan.usage.cost_usd}（{plan.usage.model}）{!plan.usage.usage_known && '（供應商未回報用量，以預留量計）'}
      </p>
      <h4>推薦架構（模型推測）</h4>
      <p>{doc.architecture.summary}</p>
      <p className="muted">
        {doc.reference_analysis === null ? (
          '未使用參考 repo，以下只依需求與配置規劃。'
        ) : (
          <>
            參考 repo：
            {doc.reference_analysis.detected !== null && (
              <>事實（系統偵測）{doc.reference_analysis.detected.language} / {doc.reference_analysis.detected.framework ?? '未偵測到框架'}；</>
            )}
            推測 架構模式 {doc.reference_analysis.architecture_pattern}、層級 {doc.reference_analysis.layer_count ?? '未知'}、核心模組{' '}
            {doc.reference_analysis.core_modules.join('、') || '未識別'}
          </>
        )}
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
