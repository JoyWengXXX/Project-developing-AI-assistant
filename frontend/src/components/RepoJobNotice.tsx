import { useEffect, useId, useRef, useState } from 'react'
import { api, type ApiError } from '../api'
import { toApiError } from '../format'
import { failureReason, JOB_KIND_LABEL, jobTitle, progressSummary } from '../jobFormat'
import { isSuperseded, jobCommitSha, type RepoJobs } from '../jobsOverview'
import type { Job, SnapshotItem } from '../types'
import { ErrorBox } from '../ui'
import type { JobControls } from '../useJobsOverview'
import { JobProgress, JobProgressBar } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'

interface Props {
  /** 白話標題用的 repo 名稱（如 deds-portal）。 */
  repoName: string
  /** 這個 repo 的進行中工作與尚未忽略的失敗工作（來自頁面共用的輪詢，不另外發請求）。 */
  jobs: RepoJobs
  /** 這個 repo 已匯入完成的快照（卡片、版本區本來就載入了）；用來判斷失敗是否已被之後的成功匯入取代。還沒載入時為 null。 */
  snapshots: readonly SnapshotItem[] | null
  workerOffline: boolean
  controls: JobControls
  /** 進度已由別處顯示的進行中工作（卡片：這次操作的匯入狀態；詳情：選取 commit 的匯入區）：不重複顯示。 */
  hideActive?: (job: Job) => boolean
  /** 失敗已由別處顯示（有「重試匯入」）的工作編號：不重複顯示。 */
  hideFailedId?: number | null
}

/** 「上次背景匯入失敗」這類標題：匯入用白話的「背景匯入」，其他種類帶上種類名稱。 */
function failureLabel(job: Job): string {
  return job.kind === 'snapshot_import' ? '上次背景匯入失敗' : `上次背景工作（${JOB_KIND_LABEL[job.kind]}）失敗`
}

/**
 * 一個 repo 的背景工作提示（總覽卡片與詳情版本區共用；沒有東西可顯示時不佔空間）：
 *   - 進行中：「背景工作進行中：匯入 deds-portal 的版本 be6ab7df（42%，已處理 …）」，可展開看進度與嘗試次數。
 *   - 最近一筆尚未忽略的失敗：「上次背景匯入失敗：<繁中原因>」＋「重試」（POST /jobs/{id}/retry，先確認）＋「忽略」（只記在瀏覽器）。
 *     若這個失敗之後已有更新的成功匯入，只顯示一行淡色的「較早的失敗」與「忽略」，不再要求處理，
 *     但它仍算在頂端的「失敗 N」，所以保留一個忽略的入口，數字才對得起來。
 * 進度數字每次輪詢都會更新，所以不放 aria-live；頂端摘要列才會報讀數量變化。
 */
export function RepoJobNotice({ repoName, jobs, snapshots, workerOffline, controls, hideActive, hideFailedId = null }: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  // 按了「忽略」或「重試」後留一行說明：也讓提示區塊不會立刻消失，鍵盤焦點有地方可去
  const [note, setNote] = useState<'dismissed' | 'retried' | null>(null)

  const active = hideActive === undefined ? jobs.active : jobs.active.filter((j) => !hideActive(j))
  const latest = jobs.failed[0] ?? null
  // 快照列表還沒載入時無法判斷是否已被取代，先不顯示（避免先閃出紅色提示又消失）
  const failure = latest !== null && latest.id !== hideFailedId && snapshots !== null ? latest : null
  // 重試後工作已出現在「進行中」：進行中的那一行取代這句說明（在 render 期間同步調整，不需要 effect）
  if (note === 'retried' && active.length > 0) setNote(null)
  if (active.length === 0 && failure === null && note === null) return null

  function afterAction(kind: 'dismissed' | 'retried') {
    setNote(kind)
    rootRef.current?.focus() // 剛按的按鈕即將消失：焦點移到這個提示區塊，不掉到頁面最上面
  }

  return (
    <div className="repo-job-notice" ref={rootRef} tabIndex={-1} role="group" aria-label="背景工作狀態">
      {active.map((job) => (
        <ActiveLine key={job.id} job={job} repoName={repoName} workerOffline={workerOffline} />
      ))}
      {failure !== null && (
        <FailureLine
          key={failure.id}
          job={failure}
          repoName={repoName}
          superseded={isSuperseded(failure, snapshots ?? [])}
          others={jobs.failed.length - 1}
          controls={controls}
          onDone={afterAction}
        />
      )}
      {note !== null && failure === null && (
        <p className="note job-notice-note" role="status">
          {note === 'dismissed'
            ? '已忽略這筆失敗（頂端的「失敗」數量也不再計入）。要恢復提醒，請在「查看所有背景工作」按「取消忽略」。'
            : '已重新排入，等待背景執行器處理。'}
        </p>
      )}
    </div>
  )
}

function ActiveLine({ job, repoName, workerOffline }: { job: Job; repoName: string; workerOffline: boolean }) {
  const [expanded, setExpanded] = useState(false)
  const detailId = `job-notice-${useId()}`
  return (
    <div className="job-notice job-notice-active">
      <div className="job-notice-line">
        <JobStatusBadge status={job.status} />
        <span>
          背景工作進行中：{jobTitle(job, repoName)}（{progressSummary(job, { workerOffline })}）
        </span>
        <button
          type="button"
          className="linklike"
          aria-expanded={expanded}
          aria-controls={expanded ? detailId : undefined}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? '收合' : '詳情'}
        </button>
      </div>
      {expanded ? (
        <div id={detailId} className="job-notice-detail">
          <JobProgress job={job} />
          <p className="job-meta muted">
            工作 #{job.id} · 嘗試 {job.attempt} / {job.max_attempts}
          </p>
          {job.status === 'queued' && job.error !== null && (
            <p className="job-error job-error-previous">
              <strong>上次嘗試失敗（會自動重試）：</strong>
              {failureReason(job)}
            </p>
          )}
          {job.status === 'queued' && workerOffline && (
            <p className="warning" role="status">
              <strong>
                <span aria-hidden="true">⚠ </span>背景執行器目前不在線，排隊中的工作暫時不會執行。
              </strong>
            </p>
          )}
        </div>
      ) : (
        <JobProgressBar job={job} />
      )}
    </div>
  )
}

interface FailureLineProps {
  job: Job
  repoName: string
  superseded: boolean
  /** 同一個 repo 還有幾個未忽略的失敗（較舊的）。 */
  others: number
  controls: JobControls
  onDone: (kind: 'dismissed' | 'retried') => void
}

function FailureLine({ job, repoName, superseded, others, controls, onDone }: FailureLineProps) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const retryRef = useRef<HTMLButtonElement>(null)
  const keepRef = useRef<HTMLButtonElement>(null)
  const wasConfirming = useRef(false)
  const title = jobTitle(job, repoName)
  const sha = jobCommitSha(job)?.slice(0, 8)

  // 鍵盤焦點（與工作列表的確認區塊一致）：開啟確認時移到「不重試」，關閉後回到「重試」
  useEffect(() => {
    if (confirming) keepRef.current?.focus()
    else if (wasConfirming.current) retryRef.current?.focus()
    wasConfirming.current = confirming
  }, [confirming])

  async function retry() {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await api<Job>('POST', `/api/v1/jobs/${job.id}/retry`)
      controls.reload() // 工作回到排隊：提示會換成「背景工作進行中」
      wasConfirming.current = false // 不要把焦點還給「重試」鈕：它馬上會消失，焦點由父元件接手
      setConfirming(false)
      onDone('retried')
    } catch (e) {
      const apiError = toApiError(e)
      setError(apiError)
      setConfirming(false)
      // 404（已不存在）、409（狀態已變，例如已被別處重試）：畫面上的資料已過期，立刻重抓
      if (apiError.status === 404 || apiError.status === 409) controls.reload()
    } finally {
      setBusy(false)
    }
  }

  function dismiss() {
    controls.dismiss(job.id)
    onDone('dismissed')
  }

  if (superseded) {
    return (
      <p className="note job-notice job-notice-old">
        較早的背景匯入失敗（之後已有成功的匯入）{sha !== undefined && `，版本 ${sha}`}：{failureReason(job)}{' '}
        <button type="button" className="linklike" onClick={dismiss}>
          忽略
        </button>
      </p>
    )
  }

  return (
    <div className="job-notice job-notice-failed">
      <p className="job-error">
        <strong>{failureLabel(job)}：</strong>
        {failureReason(job)}
        {sha !== undefined && <span className="job-notice-code">（版本 {sha}）</span>}
      </p>
      {others > 0 && <p className="note job-notice-note">這個 repo 另有 {others} 個未忽略的失敗工作，請見「查看所有背景工作」。</p>}
      <div className="action-row">
        <button
          type="button"
          className="small"
          ref={retryRef}
          aria-expanded={confirming}
          disabled={busy}
          onClick={() => setConfirming(true)}
        >
          {busy ? '重試中…' : '重試'}
        </button>
        <button
          type="button"
          className="secondary small"
          disabled={busy}
          title="不再計入頂端的「失敗」數量與這個提示（只記在這個瀏覽器，工作本身不變）"
          onClick={dismiss}
        >
          忽略
        </button>
      </div>
      {confirming && (
        <div
          className="job-confirm"
          role="group"
          aria-label={`確認重試：${title}`}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && !busy) {
              e.preventDefault()
              e.stopPropagation()
              setConfirming(false)
            }
          }}
        >
          <p>
            <strong>確定要重試「{title}」嗎？</strong> 會重新排入背景執行器，再做一次。
          </p>
          <div className="action-row">
            <button type="button" className="small" disabled={busy} onClick={() => void retry()}>
              {busy ? '重試中…' : '確定重試'}
            </button>
            <button type="button" className="secondary small" ref={keepRef} disabled={busy} onClick={() => setConfirming(false)}>
              不重試
            </button>
          </div>
        </div>
      )}
      {error !== null && (
        <div>
          <p className="note">重試失敗：</p>
          <ErrorBox error={error} />
        </div>
      )}
    </div>
  )
}
