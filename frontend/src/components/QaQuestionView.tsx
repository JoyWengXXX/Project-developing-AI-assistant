import { useEffect, useState } from 'react'
import { api } from '../api'
import { JOB_PHASE_LABEL, queueWarning } from '../jobFormat'
import { QA_STATUS_BADGE, QA_STATUS_LABEL, isQaActive } from '../qaQuestion'
import type { Job, JobsStatus, QaEstimate, QaQuestion, QaQuestionStatus, QaQuestionSubmitResponse } from '../types'
import { ApiHint } from '../ui'
import { useAction } from '../useAction'
import { usePolling, type PollState } from '../usePolling'
import { useSnapshotPresence } from '../useSnapshotPresence'
import { formatTokens, formatUsd } from '../qaFormat'
import { CancelJobButton } from './CancelJobButton'
import { Elapsed } from './JobTimes'
import { PollErrorBox } from './PollErrorBox'
import { QaAnswer } from './QaAnswer'
import { QaErrorBox } from './QaErrorBox'
import { QaContextChunks, QaQuestionMeta } from './QaQuestionMeta'

const POLL_MS = 2_000

interface Props {
  id: number
  /** 剛送出時後端回的預估（只有這一輪看得到；重新整理後以問答本身記錄的預估為準）。 */
  submitEstimate: QaEstimate | null
  /** GET /jobs/status（App 共用的那一份）：排隊但 worker 不在線時提示。 */
  jobsStatus: PollState<JobsStatus>
  /** 問答的狀態有變（第一次載入、進入終態）：呼叫端重新讀取額度與歷史，並知道目前檢視的問答是否還在進行。呼叫端須提供穩定的函式。 */
  onChanged: (status: QaQuestionStatus) => void
  /** 以同一個問題與版本重新提問成功（新的問答 id）：呼叫端改為檢視新的問答。 */
  onResubmitted: (response: QaQuestionSubmitResponse) => void
}

function ResubmitButton({ question, onResubmitted }: { question: QaQuestion; onResubmitted: (response: QaQuestionSubmitResponse) => void }) {
  const action = useAction()
  async function resubmit() {
    await action.run(async () => {
      const res = await api<QaQuestionSubmitResponse>('POST', '/api/v1/qa/questions', {
        body: { snapshot_ids: question.snapshot_ids, question: question.question, max_tokens: question.max_tokens },
      })
      onResubmitted(res.data)
    })
  }
  return (
    <div className="qa-resubmit">
      <p className="note">
        重新提問會用同樣的版本與問題建立一筆新的問答（重新檢索、重新預留額度，這次的用量另外計算）；原本這筆保留在歷史裡。
        請先處理上面的失敗原因再重試。
      </p>
      <button type="button" disabled={action.busy} onClick={() => void resubmit()}>
        {action.busy ? '送出中…' : '重新提問'}
      </button>
      <QaErrorBox error={action.error} title="無法重新提問" />
    </div>
  )
}

function newer(a: Job | null, b: Job | null): Job | null {
  if (a === null || b === null) return a ?? b
  return Date.parse(b.updated_at) > Date.parse(a.updated_at) ? b : a
}

function ActiveStatus({ question, job, jobsStatus }: { question: QaQuestion; job: Job | null; jobsStatus: PollState<JobsStatus> }) {
  const warning = question.status === 'queued' && jobsStatus.data !== null ? queueWarning(jobsStatus.data) : null
  const cancelling = job?.cancel_requested === true
  return (
    <div className="qa-active" role="status" aria-live="polite">
      <p>
        <span className={`badge ${QA_STATUS_BADGE[question.status]}`}>{QA_STATUS_LABEL[question.status]}</span>{' '}
        {question.status === 'queued' && '排隊中，等待背景執行器接手…'}
        {question.status === 'running' && (job !== null ? `${JOB_PHASE_LABEL[job.phase]}…` : '處理中…')}
        {cancelling && '（已要求取消，等目前這一步結束就會停止）'}
        {question.created_at && (
          <span className="muted">
            {' '}・已等待 <Elapsed sinceIso={question.created_at} />
          </span>
        )}
      </p>
      {job !== null && job.attempt > 1 && <p className="muted">背景工作第 {job.attempt} 次嘗試（供應商暫時性錯誤時會自動重試）</p>}
      {warning !== null && <p className="warning">{warning}</p>}
    </div>
  )
}

/**
 * 一筆問答的檢視：排隊／執行中時每 2 秒輪詢（usePolling：頁面隱藏時暫停、回到前景立刻補抓、失敗退避），
 * 到終態就停止輪詢並保留最後一次的結果。排隊與執行中可取消（POST /jobs/{id}/cancel）。
 * 完成後顯示答案、費用與追蹤資訊；呼叫端用 key={id} 換問答時整個重建。
 * 問題與答案都是不可信內容，一律當文字節點顯示。
 */
export function QaQuestionView({ id, submitEstimate, jobsStatus, onChanged, onResubmitted }: Props) {
  const [final, setFinal] = useState<QaQuestion | null>(null)
  const [gone, setGone] = useState(false)
  const [reload, setReload] = useState(0)
  const [cancelResult, setCancelResult] = useState<Job | null>(null)
  const path = `/api/v1/qa/questions/${id}`
  const poll = usePolling<QaQuestion>(path, {
    enabled: final === null && !gone,
    reloadKey: reload,
    intervalMs: () => POLL_MS,
  })

  // 進入終態：保留這一份並停止輪詢（在 render 期間同步，與專案其他 hook 相同）
  if (final === null && poll.data !== null && !isQaActive(poll.data.status)) setFinal(poll.data)
  if (!gone && poll.error?.code === 'qa_question_not_found') setGone(true)

  const question = final ?? poll.data
  const active = question !== null && isQaActive(question.status)
  const jobId = question?.job_id ?? null
  const jobPoll = usePolling<Job>(active && jobId !== null ? `/api/v1/jobs/${jobId}` : null, {
    enabled: active,
    reloadKey: reload,
    intervalMs: () => POLL_MS,
  })
  const job = newer(jobPoll.data, cancelResult)

  // 狀態有變（第一次載入、進入終態）：通知呼叫端更新額度與歷史
  const status = question?.status ?? null
  useEffect(() => {
    if (status !== null) onChanged(status)
  }, [status, onChanged])

  const presence = useSnapshotPresence(
    question === null
      ? []
      : [
          ...question.statements.flatMap((s) => s.citations.map((c) => c.snapshot_id)),
          ...question.context_chunks.map((c) => c.snapshot_id),
        ],
  )

  function afterCancel(next: Job) {
    setCancelResult(next)
    setReload((value) => value + 1) // 問答與工作立刻重新讀取（排隊中的會馬上變成已取消）
  }

  if (gone) {
    return <p className="warning" role="status">問答 #{id} 已不存在（可能已被刪除）。</p>
  }

  return (
    <div className="qa-question-view">
      <ApiHint apis={[`GET ${path}`, 'GET /api/v1/jobs/{job_id}', 'POST /api/v1/jobs/{job_id}/cancel', 'POST /api/v1/qa/questions（重新提問）']} />
      {question === null && poll.loading && <p role="status">載入問答 #{id} 中…</p>}
      <PollErrorBox state={poll} />
      {question !== null && (
        <>
          <h3>
            問答 #{question.id}
            {!active && (
              <>
                {' '}
                <span className={`badge ${QA_STATUS_BADGE[question.status]}`}>{QA_STATUS_LABEL[question.status]}</span>
              </>
            )}
          </h3>
          <blockquote className="qa-question-text">{question.question}</blockquote>
          {submitEstimate !== null && active && (
            <p className="note">
              提問時的上限估計：最多 {formatTokens(submitEstimate.total_tokens_upper_bound)} token（輸入約{' '}
              {formatTokens(submitEstimate.input_tokens_upper_bound)}、輸出最多 {formatTokens(submitEstimate.max_output_tokens)}），約{' '}
              {formatUsd(submitEstimate.cost_usd)} 美元（假設值）。
            </p>
          )}
          {active && <ActiveStatus question={question} job={job} jobsStatus={jobsStatus} />}
          {active && job !== null && (
            <CancelJobButton job={job} label="取消這次問答" onResult={afterCancel} />
          )}
          {question.status === 'failed' && (
            <>
              <QaErrorBox
                title="問答失敗"
                error={question.error ?? { code: question.error_code ?? 'job_failed', message: '問答失敗，但後端沒有回報原因。' }}
              />
              <ResubmitButton question={question} onResubmitted={onResubmitted} />
            </>
          )}
          {question.status === 'cancelled' && (
            <>
              <p className="note" role="status">
                這次問答已取消。已實際花費的用量仍計入今日額度，沒用到的預留已釋放。
              </p>
              <ResubmitButton question={question} onResubmitted={onResubmitted} />
            </>
          )}
          {question.status === 'succeeded' && <QaAnswer question={question} presence={presence} />}
          {!active && (
            <>
              <p className="muted qa-brief">
                {question.status === 'succeeded' && `約花費 ${formatUsd(question.cost.actual_usd ?? '0')} 美元`}
                {question.duration_ms !== null && `${question.status === 'succeeded' ? '・' : ''}耗時 ${(question.duration_ms / 1000).toFixed(1)} 秒`}
              </p>
              <details className="qa-technical">
                <summary>詳細資訊（模型、用量、送出的片段，給工程人員看）</summary>
                <QaQuestionMeta question={question} />
                <h4>送出的片段（{question.context_chunks.length} 個）</h4>
                <p className="note">只列出來源位置；已送出的有代號（引用用），沒送出的說明原因。片段內容不在記錄中。</p>
                <QaContextChunks question={question} presence={presence} />
              </details>
            </>
          )}
        </>
      )}
    </div>
  )
}
