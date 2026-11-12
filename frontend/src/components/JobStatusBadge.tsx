import { JOB_STATUS_BADGE, JOB_STATUS_LABEL } from '../jobFormat'
import type { JobStatus } from '../types'

const STATUS_SYMBOL: Record<JobStatus, string> = {
  queued: '⏳',
  running: '▶',
  succeeded: '✔',
  failed: '✖',
  cancelled: '⊘',
}

/** 工作狀態徽章：顏色＋符號＋文字並用。 */
export function JobStatusBadge({ status }: { status: JobStatus }) {
  return (
    <span className={`badge ${JOB_STATUS_BADGE[status]}`}>
      <span aria-hidden="true">{STATUS_SYMBOL[status]} </span>
      {JOB_STATUS_LABEL[status]}
    </span>
  )
}
