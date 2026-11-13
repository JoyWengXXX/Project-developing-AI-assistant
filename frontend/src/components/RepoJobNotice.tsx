import { useEffect, useId, useRef, useState } from 'react'
import { api, type ApiError } from '../api'
import { toApiError } from '../format'
import { githubRepoOf, isGitHubJob } from '../githubJobs'
import { resubmitImport } from '../jobActions'
import {
  canCancel,
  cancellingWait,
  failureReason,
  hasFailureDetails,
  isCancelling,
  JOB_BACKGROUND_LABEL,
  jobTitle,
  progressSummary,
} from '../jobFormat'
import { isSuperseded, jobCommitSha, type RepoJobs } from '../jobsOverview'
import type { Job, SnapshotItem } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import type { JobControls } from '../useJobsOverview'
import { useTrackedJob } from '../useTrackedJob'
import { CancelJobButton } from './CancelJobButton'
import { GitHubJobProgress, QueuedOfflineWarning } from './GitHubJobProgress'
import { JobFailureInfo } from './JobFailureInfo'
import { JobProgress, JobProgressBar } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'

interface Props {
  /** 白話標題用的 repo 名稱（如 deds-portal）。 */
  repoName: string
  /** 這個 repo 的進行中工作與尚未忽略的失敗工作（來自頁面共用的輪詢，不另外發請求）。 */
  jobs: RepoJobs
  /** 這個 repo 已匯入完成的快照（卡片、版本區本來就載入了）；用來判斷匯入失敗是否已被之後的成功匯入取代。還沒載入時為 null。 */
  snapshots: readonly SnapshotItem[] | null
  workerOffline: boolean
  controls: JobControls
  /** 進度已由別處顯示的進行中工作（卡片：這次操作的匯入狀態；詳情：選取 commit 的匯入區）：不重複顯示。 */
  hideActive?: (job: Job) => boolean
  /** 失敗已由別處顯示（有「重試匯入」）的工作編號：不重複顯示。 */
  hideFailedId?: number | null
  /** 有標題就當成獨立的區塊（「GitHub 註冊工作」：註冊成功前沒有 repo，不屬於任何卡片），否則是卡片上的一小塊提示。 */
  title?: string
  /** 取消的註冊工作要「重新註冊」時呼叫（只有註冊工作會用到）；沒有就不顯示按鈕。回傳是否排入了背景工作；失敗會拋出 ApiError，由這裡顯示。 */
  registerAgain?: (githubRepo: string) => Promise<boolean>
}

/** 「上次背景匯入失敗」「上次背景更新失敗」「上次背景註冊失敗」。 */
function failureLabel(job: Job): string {
  return `上次${JOB_BACKGROUND_LABEL[job.kind]}失敗`
}

/**
 * 背景工作提示（總覽卡片與詳情版本區共用，也用來列出尚無 repo 的 GitHub 註冊；沒有東西可顯示時不佔空間）。
 * 匯入、GitHub 更新與註冊的文案依種類區分（背景匯入／背景更新／背景註冊）：
 *   - 進行中：「背景匯入進行中：匯入 deds-portal 的版本 be6ab7df（42%，已處理 …）」，可展開看進度與嘗試次數，
 *     也可「取消」（先確認）。GitHub 工作直接顯示階段、已耗時與已接收量（GitHubJobProgress）。
 *     取消後這一行換成「取消中…」，工作停下來後顯示「已取消」與「重新匯入」（匯入：同一個工作重新排入）或「重新註冊」。
 *   - 最近一筆尚未忽略的失敗：「上次背景匯入失敗」＋失敗追查資訊（JobFailureInfo）＋「重試」（POST /jobs/{id}/retry，先確認）＋「忽略」（只記在瀏覽器）。
 *     若匯入的失敗之後已有更新的成功匯入，只顯示一行淡色的「較早的失敗」與「忽略」，不再要求處理，
 *     但它仍算在頂端的「失敗 N」，所以保留一個忽略的入口，數字才對得起來。
 * 進度數字每次輪詢都會更新，所以不放 aria-live（GitHubJobProgress 只有階段那一行是 live）；頂端摘要列才會報讀數量變化。
 */
export function RepoJobNotice({
  repoName,
  jobs,
  snapshots,
  workerOffline,
  controls,
  hideActive,
  hideFailedId = null,
  title,
  registerAgain,
}: Props) {
  const rootRef = useRef<HTMLDivElement>(null)
  // 按了「忽略」或「重試」後留一行說明：也讓提示區塊不會立刻消失，鍵盤焦點有地方可去
  const [note, setNote] = useState<'dismissed' | 'retried' | 'registered' | null>(null)
  // 在這裡按了取消的工作：它離開共用輪詢的「進行中」清單後，仍由 CancelledLine 追蹤到結束並顯示結果，直到使用者關閉
  const [cancelled, setCancelled] = useState<Job[]>([])
  const trackedIds = new Set(cancelled.map((j) => j.id))

  const active = (hideActive === undefined ? jobs.active : jobs.active.filter((j) => !hideActive(j))).filter((j) => !trackedIds.has(j.id))
  const failedJobs = jobs.failed.filter((j) => !trackedIds.has(j.id))
  const latest = failedJobs[0] ?? null
  // 快照列表還沒載入時無法判斷是否已被取代，先不顯示（避免先閃出紅色提示又消失）
  const failure = latest !== null && latest.id !== hideFailedId && snapshots !== null ? latest : null
  // 重試後工作已出現在「進行中」：進行中的那一行取代這句說明（在 render 期間同步調整，不需要 effect）
  if (note === 'retried' && active.length > 0) setNote(null)
  if (active.length === 0 && failure === null && note === null && cancelled.length === 0) return null

  function afterAction(kind: 'dismissed' | 'retried' | 'registered') {
    setNote(kind)
    rootRef.current?.focus() // 剛按的按鈕即將消失：焦點移到這個提示區塊，不掉到頁面最上面
  }

  function handleCancelled(job: Job) {
    setCancelled((prev) => (prev.some((j) => j.id === job.id) ? prev : [...prev, job]))
    controls.reload()
    rootRef.current?.focus() // 這一行即將換成「取消中…／已取消」，原本的按鈕不在了
  }

  function closeCancelled(jobId: number) {
    setCancelled((prev) => prev.filter((j) => j.id !== jobId))
  }

  // 重新匯入／重新註冊成功：匯入是同一個工作回到排隊、註冊是新的工作，都由共用輪詢接手顯示
  // （這裡先留一行說明，直到它出現在「進行中」）；註冊若沒有建立工作（已存在、同步回退）就說明已完成
  function requeued(jobId: number, queued: boolean) {
    closeCancelled(jobId)
    afterAction(queued ? 'retried' : 'registered')
  }

  return (
    <div
      className={title === undefined ? 'repo-job-notice' : 'repo-job-notice panel'}
      ref={rootRef}
      tabIndex={-1}
      role="group"
      aria-label={title ?? '背景工作狀態'}
    >
      {title !== undefined && <h3 className="job-notice-heading">{title}</h3>}
      {active.map((job) => (
        <ActiveLine key={job.id} job={job} repoName={repoName} workerOffline={workerOffline} onCancelled={handleCancelled} />
      ))}
      {cancelled.map((job) => (
        <CancelledLine
          key={job.id}
          initial={job}
          repoName={repoName}
          controls={controls}
          registerAgain={registerAgain}
          onClose={closeCancelled}
          onRequeued={requeued}
        />
      ))}
      {failure !== null && (
        <FailureLine
          key={failure.id}
          job={failure}
          repoName={repoName}
          superseded={failure.kind === 'snapshot_import' && isSuperseded(failure, snapshots ?? [])}
          othersNote={
            failedJobs.length > 1
              ? `${title === undefined ? '這個 repo' : '這裡'}另有 ${failedJobs.length - 1} 個未忽略的失敗工作，請見「查看所有背景工作」。`
              : null
          }
          controls={controls}
          onDone={afterAction}
        />
      )}
      {note !== null && failure === null && (
        <p className="note job-notice-note" role="status">
          {note === 'dismissed' && '已忽略這筆失敗（頂端的「失敗」數量也不再計入）。要恢復提醒，請在「查看所有背景工作」按「取消忽略」。'}
          {note === 'retried' && '已重新排入，等待背景執行器處理。'}
          {note === 'registered' && '已重新註冊（沒有建立背景工作；結果見上方的說明與總覽）。'}
        </p>
      )}
    </div>
  )
}

function ActiveLine({
  job,
  repoName,
  workerOffline,
  onCancelled,
}: {
  job: Job
  repoName: string
  workerOffline: boolean
  onCancelled: (job: Job) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const detailId = `job-notice-${useId()}`

  // GitHub 註冊與更新：直接顯示階段、已耗時與已接收量，不必展開
  if (isGitHubJob(job)) {
    return (
      <div className="job-notice job-notice-active">
        <p className="job-notice-title">
          <strong>{JOB_BACKGROUND_LABEL[job.kind]}進行中：</strong>
          {jobTitle(job, repoName)}
        </p>
        <GitHubJobProgress job={job} workerOffline={workerOffline} />
        <p className="job-meta muted">工作 #{job.id}</p>
        {canCancel(job) && <CancelJobButton job={job} label="取消" small onResult={onCancelled} />}
      </div>
    )
  }

  return (
    <div className="job-notice job-notice-active">
      <div className="job-notice-line">
        <JobStatusBadge status={job.status} cancelling={isCancelling(job)} />
        <span>
          {JOB_BACKGROUND_LABEL[job.kind]}進行中：{jobTitle(job, repoName)}（{progressSummary(job, { workerOffline })}）
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
            工作 #{job.id}
            {!hasFailureDetails(job) && ` · 嘗試 ${job.attempt} / ${job.max_attempts}`}
          </p>
          <JobFailureInfo job={job} />
          {job.status === 'queued' && workerOffline && <QueuedOfflineWarning />}
        </div>
      ) : (
        <JobProgressBar job={job} />
      )}
      {canCancel(job) && <CancelJobButton job={job} label="取消" small onResult={onCancelled} />}
    </div>
  )
}

/**
 * 使用者在這裡按了取消的工作：追蹤到結束（useTrackedJob）並顯示結果。
 * 「取消中…」（匯入要等目前這一批處理完；GitHub 工作約數秒內停止）→「已取消」＋「重新匯入」（POST import-jobs，
 * 後端把同一個工作重新排入）或「重新註冊」（registerAgain；取消後沒有資料列也沒有目錄，可以直接重新提交）；
 * 若工作在取消生效前就結束了（成功或失敗），如實顯示那個結果。
 */
function CancelledLine({
  initial,
  repoName,
  controls,
  registerAgain,
  onClose,
  onRequeued,
}: {
  initial: Job
  repoName: string
  controls: JobControls
  registerAgain?: (githubRepo: string) => Promise<boolean>
  onClose: (jobId: number) => void
  onRequeued: (jobId: number, queued: boolean) => void
}) {
  const job = useTrackedJob(initial)
  const action = useAction()
  const sha = jobCommitSha(job)
  const githubRepo = githubRepoOf(job)
  const repositoryId = job.repository_id
  const title = jobTitle(job, repoName)

  // 取消的匯入可以重新匯入（同一個工作重新排入）；取消的註冊可以重新註冊（新的工作）；取消的更新直接再按「更新」
  // run 回傳「是否排入了背景工作」：註冊若已存在或回退成同步，就沒有工作，說明文字不同
  let again: { label: string; run: () => Promise<boolean> } | null = null
  if (job.kind === 'snapshot_import' && sha !== null && repositoryId !== null) {
    again = {
      label: '重新匯入',
      run: async () => {
        await resubmitImport(repositoryId, sha)
        return true
      },
    }
  } else if (job.kind === 'github_register' && githubRepo !== null && registerAgain !== undefined) {
    again = { label: '重新註冊', run: () => registerAgain(githubRepo) }
  }

  async function submitAgain() {
    if (again === null) return
    const submit = again.run
    let queued = true
    const ok = await action.run(async () => {
      queued = await submit()
    })
    if (ok) {
      controls.reload()
      onRequeued(job.id, queued)
    }
  }

  if (canCancel(job)) {
    return (
      <div className="job-notice job-notice-active">
        <div className="job-notice-line">
          <JobStatusBadge status={job.status} cancelling={isCancelling(job)} />
          <span>
            取消中…：{title}（{cancellingWait(job.kind)}）
          </span>
        </div>
      </div>
    )
  }

  return (
    <div className="job-notice job-notice-old">
      <div className="job-notice-line">
        <JobStatusBadge status={job.status} />
        <span>
          {job.status === 'cancelled' && `已取消：${title}`}
          {job.status === 'succeeded' && `${title}在取消生效前已經完成。`}
          {job.status === 'failed' && `${title}在取消生效前已經失敗。`}
        </span>
      </div>
      <JobFailureInfo job={job} />
      <div className="action-row">
        {job.status === 'cancelled' && again !== null && (
          <button type="button" className="small" disabled={action.busy} onClick={() => void submitAgain()}>
            {action.busy ? '送出中…' : again.label}
          </button>
        )}
        <button type="button" className="secondary small" onClick={() => onClose(job.id)}>
          關閉
        </button>
      </div>
      <ErrorBox error={action.error} />
    </div>
  )
}

interface FailureLineProps {
  job: Job
  repoName: string
  superseded: boolean
  /** 同一處還有其他未忽略的失敗（較舊的）時的說明；沒有就是 null。 */
  othersNote: string | null
  controls: JobControls
  onDone: (kind: 'dismissed' | 'retried') => void
}

function FailureLine({ job, repoName, superseded, othersNote, controls, onDone }: FailureLineProps) {
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
      <p className="job-notice-title">
        <strong>{failureLabel(job)}</strong>
        {sha !== undefined && <span className="job-notice-code">（版本 {sha}）</span>}
      </p>
      <JobFailureInfo job={job} />
      {othersNote !== null && <p className="note job-notice-note">{othersNote}</p>}
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
