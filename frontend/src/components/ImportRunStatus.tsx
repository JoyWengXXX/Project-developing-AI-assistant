import { JOB_PHASE_LABEL } from '../jobFormat'
import type { ImportRun } from '../useRepoImports'
import { ErrorBox } from '../ui'
import { methodNote } from '../versionJobs'
import { JobProgress } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'

interface Props {
  run: ImportRun
  /** 匯入進行中時，不能再開始別的匯入（一次一個）；失敗後的「重試」也受此限。 */
  locked: boolean
  onRetry: () => void
  onDismiss: () => void
}

/**
 * 卡片上一次匯入的狀態：進行中（背景工作的階段與進度，或同步匯入的等待說明）、失敗（錯誤與重試）。
 * 完成（done）的結果不在這裡顯示：卡片會重新讀取概況，直接顯示新的內容。
 */
export function ImportRunStatus({ run, locked, onRetry, onDismiss }: Props) {
  if (run.status === 'done') return null

  if (run.status === 'failed') {
    return (
      <div className="import-run">
        <p className="note">
          {run.errorStep === 'reference' ? '匯入已完成，但設為參考版本失敗：' : '匯入失敗：'}
        </p>
        <ErrorBox error={run.error} />
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
  let summary: string
  if (job !== null) {
    summary = job.status === 'queued' ? '排隊中，等候 worker 取用' : '正在匯入此版本'
  } else if (method?.kind === 'sync') {
    summary = '正在同步匯入…第一次匯入會讀取所有檔案並保存，大型 repo 可能需要數秒到數分鐘'
  } else {
    summary = '正在送出匯入…'
  }

  return (
    <div className="import-run">
      <p className="import-run-summary" role="status" aria-busy="true">
        {job !== null && <JobStatusBadge status={job.status} />} {summary}
        {job !== null && `（階段：${JOB_PHASE_LABEL[job.phase]}）`}
      </p>
      {job !== null && <JobProgress job={job} />}
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
