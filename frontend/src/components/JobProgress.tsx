import { batchText, formatSize, progressPercent, progressText } from '../jobFormat'
import type { Job } from '../types'
import { Elapsed } from './JobTimes'

/**
 * 只有進度條（已知總數才有；總數未知的執行中工作畫一條「不確定」的動態條，沒有任何百分比）。
 * 沒有東西可畫時回傳 null。工作列表與 JobProgress 共用。
 */
export function JobProgressBar({ job }: { job: Job }) {
  const { processed_files: done, total_files: total } = job.progress
  const pct = progressPercent(job.progress)
  if (pct !== null && total !== null) {
    return (
      <div
        className="progress-track"
        role="progressbar"
        aria-label={`工作 #${job.id} 進度`}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-valuenow={Math.min(done, total)}
        aria-valuetext={`已處理 ${done} / ${total} 個檔案（${pct}%）`}
      >
        <div className="progress-fill" style={{ width: `${pct}%` }} />
      </div>
    )
  }
  if (job.status === 'running') {
    return (
      <div
        className="progress-track indeterminate"
        role="progressbar"
        aria-label={`工作 #${job.id} 進度`}
        aria-busy="true"
        aria-valuetext="進行中，總數未知"
      >
        <div className="progress-fill" />
      </div>
    )
  }
  return null
}

/**
 * 工作進度：已知總數才畫進度條與百分比；total_files 為 null（總數未知）時不顯示百分比——
 * 匯入顯示「正在列舉檔案…」，其他工作只顯示已處理數；執行中另畫一條「不確定」的動態條（沒有 aria-valuenow）。
 * 文字與批次（「第 3／21 批」）的規則在 jobFormat.ts。
 */
export function JobProgress({ job }: { job: Job }) {
  const p = job.progress
  const running = job.status === 'running'
  const batch = batchText(job)

  return (
    <div className="job-progress">
      <JobProgressBar job={job} />
      <div className="job-progress-text">
        <span>{progressText(job)}</span>
        {batch !== null && <span className="muted job-batch">{batch}</span>}
        {p.indexed_files > 0 && <span>已建立索引 {p.indexed_files} 個檔案</span>}
        {p.parse_error_count > 0 && <span>解析錯誤 {p.parse_error_count}</span>}
        {p.bytes_received !== null && <span>已接收約 {formatSize(p.bytes_received)}（暫存目錄大小，非百分比）</span>}
        {running && job.phase_started_at !== null && (
          <span className="muted">
            此階段已進行 <Elapsed sinceIso={job.phase_started_at} />
          </span>
        )}
      </div>
    </div>
  )
}
