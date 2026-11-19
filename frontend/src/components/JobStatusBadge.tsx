import { JOB_STATUS_BADGE, JOB_STATUS_LABEL } from '../jobFormat'
import type { JobStatus } from '../types'

const STATUS_SYMBOL: Record<JobStatus, string> = {
  queued: '⏳',
  running: '▶',
  succeeded: '✔',
  failed: '✖',
  cancelled: '⊘',
}

/**
 * 工作狀態徽章：顏色＋符號＋文字並用。
 * cancelling：執行中的工作已要求取消、還沒停下來——徽章改顯示「取消中…」（後端的 status 仍是 running）。
 */
export function JobStatusBadge({ status, cancelling = false }: { status: JobStatus; cancelling?: boolean }) {
  if (cancelling) {
    return (
      <span className="badge badge-importing">
        <span aria-hidden="true">{STATUS_SYMBOL.cancelled} </span>取消中…
      </span>
    )
  }
  return (
    <span className={`badge ${JOB_STATUS_BADGE[status]}`}>
      <span aria-hidden="true">{STATUS_SYMBOL[status]} </span>
      {JOB_STATUS_LABEL[status]}
    </span>
  )
}
