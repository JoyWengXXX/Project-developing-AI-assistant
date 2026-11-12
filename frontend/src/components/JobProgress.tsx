import { formatSize, progressPercent } from '../jobFormat'
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
 * 工作進度：已知總數才畫進度條與百分比；total_files 為 null（總數未知）時只顯示「已處理 N 個」，
 * 執行中另畫一條「不確定」的動態條（沒有 aria-valuenow，也不顯示任何百分比）。
 */
export function JobProgress({ job }: { job: Job }) {
  const p = job.progress
  const done = p.processed_files
  const total = p.total_files
  const pct = progressPercent(p)
  const running = job.status === 'running'

  let text: string
  if (total !== null && total > 0) text = `${done} / ${total} 個檔案（${pct}%）`
  else if (total === 0) text = `沒有檔案需要處理（${done} / 0）`
  else if (done > 0) text = `已處理 ${done} 個檔案（總數未知）`
  else if (running) text = '進行中（總數未知，無法估計進度）'
  else if (job.status === 'queued') text = '尚未開始'
  else text = '沒有進度資料'

  return (
    <div className="job-progress">
      <JobProgressBar job={job} />
      <div className="job-progress-text">
        <span>{text}</span>
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
