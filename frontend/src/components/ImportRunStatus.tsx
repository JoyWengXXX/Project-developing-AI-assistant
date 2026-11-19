import { useRef } from 'react'
import { useFocusIfLost } from '../useFocusIfLost'
import { canCancel, isCancelling, JOB_PHASE_LABEL } from '../jobFormat'
import type { Job } from '../types'
import type { ImportRun } from '../useRepoImports'
import { ErrorBox } from '../ui'
import { methodNote } from '../versionJobs'
import { CancelJobButton } from './CancelJobButton'
import { JobFailureInfo } from './JobFailureInfo'
import { JobProgress } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'

interface Props {
  run: ImportRun
  /** 匯入進行中時，不能再開始別的匯入（一次一個）；失敗後的「重試」、取消後的「重新匯入」也受此限。 */
  locked: boolean
  onRetry: () => void
  onDismiss: () => void
  /** 取消請求成功後（POST /jobs/{id}/cancel 回來的最新 job）：併入這次匯入的狀態。 */
  onCancelResult: (job: Job) => void
}

/**
 * 卡片上一次匯入的狀態：進行中（背景工作的階段與進度，或同步匯入的等待說明；背景工作可「取消」，同步匯入不行）、
 * 已取消（可「重新匯入」）、失敗（錯誤與重試）。
 * 完成（done）的結果不在這裡顯示：卡片會重新讀取概況，直接顯示新的內容。
 */
export function ImportRunStatus({ run, locked, onRetry, onDismiss, onCancelResult }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  // 執行中的取消要等 worker 停下來：「取消中…」變成「已取消」時，原本的按鈕消失，焦點移到這一區
  useFocusIfLost(rootRef, run.status === 'cancelled')
  if (run.status === 'done') return null

  // 取消成功後，原本的「取消」可能已不在畫面上（立即取消）：把焦點移到這一區，鍵盤使用者不會掉到頁面最上方
  function handleCancelResult(result: Job) {
    onCancelResult(result)
    rootRef.current?.focus()
  }

  if (run.status === 'cancelled') {
    return (
      <div className="import-run" ref={rootRef} tabIndex={-1}>
        <p className="import-run-summary" role="status">
          <JobStatusBadge status="cancelled" /> 已讀取的內容沒有保留，可以重新匯入。
        </p>
        {run.job !== null && <JobFailureInfo job={run.job} />}
        <div className="action-row">
          <button type="button" disabled={locked} onClick={onRetry}>
            重新匯入
          </button>
          <button type="button" className="secondary" onClick={onDismiss}>
            關閉
          </button>
        </div>
      </div>
    )
  }

  if (run.status === 'failed') {
    return (
      <div className="import-run">
        <p className="note">
          {run.errorStep === 'reference' ? '匯入已完成，但設為參考版本失敗：' : '匯入失敗：'}
        </p>
        {run.job?.status === 'failed' && run.errorStep === 'import' ? <JobFailureInfo job={run.job} /> : <ErrorBox error={run.error} />}
        <div className="action-row">
          <button type="button" disabled={locked} onClick={onRetry}>
            重試匯入
          </button>
          <button type="button" className="secondary" onClick={onDismiss}>
            關閉
          </button>
        </div>
      </div>
    )
  }

  const { job, method } = run
  const cancelling = job !== null && isCancelling(job)
  let summary: string
  if (job !== null) {
    summary = cancelling ? '正在取消這次匯入' : job.status === 'queued' ? '排隊中，等候 worker 取用' : '正在匯入此版本'
  } else if (method?.kind === 'sync') {
    summary = '正在同步匯入…第一次匯入會讀取所有檔案並保存，大型 repo 可能需要數秒到數分鐘'
  } else {
    summary = '正在送出匯入…'
  }

  return (
    <div className="import-run" ref={rootRef} tabIndex={-1}>
      <p className="import-run-summary" role="status" aria-busy="true">
        {job !== null && <JobStatusBadge status={job.status} cancelling={cancelling} />} {summary}
        {job !== null && `（階段：${JOB_PHASE_LABEL[job.phase]}）`}
      </p>
      {job !== null && <JobProgress job={job} />}
      {job !== null && <JobFailureInfo job={job} />}
      {job !== null && canCancel(job) && <CancelJobButton job={job} label="取消" small onResult={handleCancelResult} />}
      {method?.kind === 'sync' && <p className="note">同步載入無法取消。</p>}
      {run.thenReference && <p className="note">匯入完成後會自動改用這個版本當參考版本。</p>}
      {run.pollError !== null && (
        <>
          <ErrorBox error={run.pollError} />
          <p className="note">畫面上的進度可能已過期，會自動重試取得（失敗時逐步拉長間隔）。</p>
        </>
      )}
      {method !== null && <p className="note">{methodNote(method)}</p>}
    </div>
  )
}
