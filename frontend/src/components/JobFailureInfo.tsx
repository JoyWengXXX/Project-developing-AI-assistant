import { ERROR_HINT, formatTime } from '../format'
import { cancelTimeIso, isRetryExhausted, workerCrashCount } from '../jobFormat'
import type { Job } from '../types'
import { CopyRequestId } from './CopyRequestId'
import { RetryCountdown } from './JobTimes'

interface Props {
  job: Job
  /** 顯示「嘗試 a / b」與 request_id（可複製）。工作詳情本來就列出這兩項，傳 false 避免重複。 */
  showFacts?: boolean
}

/**
 * 工作的失敗追查資訊（總覽卡片、詳情匯入區、工作列表詳情共用，三處內容一致）：
 *   - 已取消：「已取消」與取消時間。
 *   - 有錯誤：error.code＋繁中說明（ERROR_HINT；沒有就用後端訊息）與後端訊息；failed 標「失敗原因」，
 *     其他狀態（重新排隊的工作保留上一次的錯誤）標「上次失敗原因」；worker_lost 且 failed＝重試額度已耗盡。
 *   - worker 曾中斷（diagnostics.poison.total_crashes > 0）、排隊中的退避倒數、嘗試次數與 request_id。
 * 沒有任何可顯示的資訊時回傳 null。所有文字（含後端訊息）都由 React 當純文字輸出，不會被當 HTML 解析。
 */
export function JobFailureInfo({ job, showFacts = true }: Props) {
  if (job.status === 'cancelled') {
    const iso = cancelTimeIso(job)
    return (
      <p className="job-outcome">
        已取消{iso !== null && <span className="muted">（{formatTime(iso)}）</span>}
      </p>
    )
  }

  const { error } = job
  const crashes = workerCrashCount(job)
  if (error === null && crashes === 0) return null

  const hint = error === null ? undefined : ERROR_HINT[error.code]
  const previous = job.status !== 'failed' // 重新排隊（或已成功）的工作保留的是「上次」的錯誤
  return (
    <div className={`job-failure job-error${previous ? ' job-error-previous' : ''}`}>
      {error !== null && (
        <>
          <p>
            <strong>{previous ? '上次失敗原因' : '失敗原因'}：</strong>
            <code>{error.code}</code> {hint ?? error.message}
          </p>
          {hint !== undefined && error.message !== '' && <p className="job-failure-detail">後端訊息：{error.message}</p>}
          {isRetryExhausted(job) && (
            <p>
              <strong>重試額度已耗盡</strong>，不會再自動重試；確認原因後可以手動重試。
            </p>
          )}
        </>
      )}
      {crashes > 0 && <p>背景程序曾中斷 {crashes} 次。</p>}
      {job.status === 'queued' && job.next_attempt_at !== null && (
        <RetryCountdown key={job.next_attempt_at} targetIso={job.next_attempt_at} />
      )}
      {showFacts && (
        <p className="job-failure-detail">
          嘗試 {job.attempt} / {job.max_attempts}
          {job.request_id !== null && (
            <>
              {' · '}request_id：<CopyRequestId requestId={job.request_id} />
            </>
          )}
        </p>
      )}
    </div>
  )
}
