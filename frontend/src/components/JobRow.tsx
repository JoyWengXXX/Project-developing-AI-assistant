import { useEffect, useId, useRef, useState } from 'react'
import {
  canCancel,
  canRetry,
  cancelExplain,
  isCancelling,
  JOB_ACTION_LABEL,
  jobOutcome,
  jobTitle,
  failureReason,
  progressPercent,
  retriedText,
  type JobAction,
} from '../jobFormat'
import type { Job } from '../types'
import { JobDetail } from './JobDetail'
import { JobProgressBar } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'
import { RelativeTime, RetryCountdown } from './JobTimes'

interface Props {
  job: Job
  /** 白話標題用的 repo 名稱（如 deds-portal）；repo 列表尚未載入或找不到時是 `#id`，工作沒有 repo 時是 null。 */
  repoShort: string | null
  /** 「詳情」裡顯示的 repo（`#6 deds-portal`）。 */
  repoFull: string
  /** 背景執行器（worker）目前不在線：排隊中的說明會改成「暫時不會執行」。 */
  workerOffline: boolean
  /** 目前送出中的取消／重試（全面板同時只會有一個）；有任何一個送出中，所有按鈕都停用。 */
  acting: { id: number; action: JobAction } | null
  /** 執行取消／重試並顯示結果（錯誤由父元件處理，不會拋出）。 */
  onAction: (job: Job, action: JobAction) => Promise<void>
  /** 失敗工作的「忽略」狀態（只有 failed 會用到）：已忽略的不計入頂端「失敗 N」與卡片提示，但仍留在這份列表。 */
  dismissal?: { dismissed: boolean; onToggle: () => void }
}

/**
 * 列表中的一筆工作，給一般使用者看的白話版：
 *   第一行＝狀態徽章＋「匯入 deds-portal 的版本 be6ab7df」＋相對時間；第二行＝結果句（進行中、已完成、失敗原因…）。
 * 階段、嘗試次數、request_id、params／result 等後台術語都收在「詳情」裡（只有發生過重試時才在主列顯示「已重試 N 次」）。
 * 已完成與已取消的工作預設精簡成單行；取消／重試先行內確認。
 */
export function JobRow({ job, repoShort, repoFull, workerOffline, acting, onAction, dismissal }: Props) {
  const [expanded, setExpanded] = useState(false)
  const [confirming, setConfirming] = useState<JobAction | null>(null)
  const detailId = `job-detail-${useId()}`
  const toggleRef = useRef<HTMLButtonElement>(null)
  const cancelRef = useRef<HTMLButtonElement>(null)
  const retryRef = useRef<HTMLButtonElement>(null)
  const keepRef = useRef<HTMLButtonElement>(null) // 確認區塊中「不取消／不重試」（安全的預設選項）
  const previousConfirming = useRef<JobAction | null>(null)

  const cancellable = canCancel(job)
  const retryable = canRetry(job)
  // 輪詢更新後若工作狀態已變（例如剛好結束），確認區塊就不再顯示
  const showConfirm =
    (confirming === 'cancel' && cancellable && !job.cancel_requested) || (confirming === 'retry' && retryable)
  const mine = acting !== null && acting.id === job.id
  const compact = job.status === 'succeeded' || job.status === 'cancelled'

  // 鍵盤焦點：開啟確認區塊時移到「不…」；關閉後回到原本的按鈕（按鈕已消失時回到「詳情」）
  useEffect(() => {
    if (confirming !== null) keepRef.current?.focus()
    else if (previousConfirming.current !== null) {
      const opener = previousConfirming.current === 'cancel' ? cancelRef.current : retryRef.current
      ;(opener ?? toggleRef.current)?.focus()
    }
    previousConfirming.current = confirming
  }, [confirming])

  async function confirm(action: JobAction) {
    await onAction(job, action)
    setConfirming(null)
  }

  const actionLabel = confirming === null ? '' : JOB_ACTION_LABEL[confirming]
  const title = jobTitle(job, repoShort)
  const retried = retriedText(job)
  const outcome = jobOutcome(job, { workerOffline })
  const showBar = job.status === 'running' && progressPercent(job.progress) !== null // 有總數才畫進度條
  const timeIso = job.finished_at ?? job.created_at
  const timeLabel = job.finished_at !== null ? '結束於' : '建立於'

  return (
    <li className={`job-item job-${job.status}${compact ? ' job-compact' : ''}`}>
      <div className="job-line">
        <JobStatusBadge status={job.status} cancelling={isCancelling(job)} />
        {dismissal?.dismissed === true && job.status === 'failed' && (
          <span className="badge badge-neutral" title="已忽略：不計入頂端的「失敗」數量與 repo 卡片的提示">
            已忽略
          </span>
        )}
        <span className="job-title">{title}</span>
        <span className="job-when muted">
          <RelativeTime iso={timeIso} label={timeLabel} />
        </span>
      </div>

      {/* 失敗用紅底（直接說原因）；其他狀態是一般的結果句 */}
      <p className={job.status === 'failed' ? 'job-error' : 'job-outcome'}>
        {outcome}
        {retried !== null && <span className="job-retried"> · {retried}</span>}
      </p>

      {showBar && <JobProgressBar job={job} />}

      {job.status === 'queued' && job.next_attempt_at !== null && (
        <RetryCountdown key={job.next_attempt_at} targetIso={job.next_attempt_at} />
      )}
      {job.status === 'queued' && job.error !== null && (
        <p className="job-error job-error-previous">
          <strong>上次嘗試失敗：</strong>
          {failureReason(job)}
        </p>
      )}

      <div className="action-row">
        <button
          type="button"
          className="secondary small"
          ref={toggleRef}
          aria-expanded={expanded}
          aria-controls={expanded ? detailId : undefined}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收合詳情' : '詳情'}
        </button>
        {cancellable && (
          <button
            type="button"
            className="danger small"
            ref={cancelRef}
            aria-expanded={confirming === 'cancel'}
            disabled={acting !== null || job.cancel_requested}
            onClick={() => setConfirming('cancel')}
          >
            {isCancelling(job) ? '取消中…' : '取消'}
          </button>
        )}
        {retryable && (
          <button
            type="button"
            className="secondary small"
            ref={retryRef}
            aria-expanded={confirming === 'retry'}
            disabled={acting !== null}
            onClick={() => setConfirming('retry')}
          >
            重試
          </button>
        )}
        {dismissal !== undefined && job.status === 'failed' && (
          <button
            type="button"
            className="secondary small"
            title={
              dismissal.dismissed
                ? '恢復計入頂端的「失敗」數量與 repo 卡片的提示'
                : '不再計入頂端的「失敗」數量與 repo 卡片的提示（只記在這個瀏覽器，工作本身不變）'
            }
            onClick={dismissal.onToggle}
          >
            {dismissal.dismissed ? '取消忽略' : '忽略'}
          </button>
        )}
      </div>

      {showConfirm && confirming !== null && (
        <div
          className="job-confirm"
          role="group"
          aria-label={`確認${actionLabel}：${title}`}
          onKeyDown={(e) => {
            // Esc 只關閉這個確認區塊；擋下預設行為與冒泡，外層的對話框不會跟著關閉
            if (e.key === 'Escape' && acting === null) {
              e.preventDefault()
              e.stopPropagation()
              setConfirming(null)
            }
          }}
        >
          <p>
            <strong>
              確定要{actionLabel}「{title}」嗎？
            </strong>{' '}
            {confirming === 'cancel' ? cancelExplain(job) : '會重新排入背景執行器，再做一次。'}
          </p>
          <div className="action-row">
            <button
              type="button"
              className={confirming === 'cancel' ? 'danger-solid small' : 'small'}
              disabled={acting !== null}
              onClick={() => void confirm(confirming)}
            >
              {mine ? `${actionLabel}中…` : `確定${actionLabel}`}
            </button>
            <button
              type="button"
              className="secondary small"
              ref={keepRef}
              disabled={acting !== null}
              onClick={() => setConfirming(null)}
            >
              不{actionLabel}
            </button>
          </div>
        </div>
      )}

      {expanded && <JobDetail job={job} repoName={repoFull} id={detailId} />}
    </li>
  )
}
