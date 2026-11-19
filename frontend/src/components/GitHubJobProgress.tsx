import { bytesReceivedText, cancellingWait, isCancelling, JOB_PHASE_LABEL } from '../jobFormat'
import type { Job } from '../types'
import { JobFailureInfo } from './JobFailureInfo'
import { JobProgressBar } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'
import { Elapsed } from './JobTimes'

interface Props {
  job: Job
  /** 背景執行器（worker）不在線：排隊中的工作要加警告。 */
  workerOffline: boolean
}

/** 排隊中而 worker 不在線的警告（與卡片提示、工作列表的說法一致）。 */
export function QueuedOfflineWarning() {
  return (
    <p className="warning" role="status">
      <strong>
        <span aria-hidden="true">⚠ </span>背景執行器目前不在線，排隊中的工作暫時不會執行。
      </strong>
    </p>
  )
}

/**
 * GitHub 註冊／更新工作的進度：階段（白話）、本階段已耗時（以 phase_started_at 即時計算）、
 * clone 期間已接收的量（暫存目錄大小，不是百分比）；沒有百分比，進度條只是「進行中」的動態條。
 * 失敗追查（JobFailureInfo）：暫時性錯誤退避重試時，顯示上次失敗原因與重試倒數。
 * 只有第一行（狀態與階段）是 aria-live：它只在階段改變時才變；已耗時每秒更新、已接收量每 2 秒更新，不放進 live 區，避免一直被朗讀。
 */
export function GitHubJobProgress({ job, workerOffline }: Props) {
  const running = job.status === 'running'
  const cancelling = isCancelling(job)
  const received = bytesReceivedText(job.progress.bytes_received)

  let headline: string
  if (cancelling) headline = `正在取消（${cancellingWait(job.kind)}）`
  else if (running) headline = JOB_PHASE_LABEL[job.phase]
  else headline = '排隊中，等候背景執行器取用'

  return (
    <div className="job-progress github-progress">
      <p className="github-progress-headline" role="status">
        <JobStatusBadge status={job.status} cancelling={cancelling} /> {headline}
      </p>
      <JobProgressBar job={job} />
      {running && (
        <div className="job-progress-text">
          {job.phase_started_at !== null && (
            <span>
              本階段已耗時 <Elapsed sinceIso={job.phase_started_at} />
            </span>
          )}
          {received !== null && <span>{received}</span>}
        </div>
      )}
      {running && job.phase === 'cloning' && (
        <p className="note">git clone 不提供百分比，所以這裡只顯示已耗時與已接收量；大型 repo 可能需要數分鐘。</p>
      )}
      <JobFailureInfo job={job} />
      {job.status === 'queued' && workerOffline && <QueuedOfflineWarning />}
    </div>
  )
}
