import { useEffect, useRef, useState } from 'react'
import { api, type ApiError } from '../api'
import { repoLabel, repoTitle, toApiError } from '../format'
import {
  cancellingWait,
  JOB_ACTION_LABEL,
  JOB_KIND_LABEL,
  JOB_STATUS_FILTERS,
  JOBS_ATTENTION,
  jobTitle,
  type JobAction,
} from '../jobFormat'
import { orderFailedForList } from '../jobsOverview'
import type { Job, JobKind, Repository } from '../types'
import { ApiHint, ErrorBox, Pager } from '../ui'
import { useJobsList } from '../useJobsList'
import { JOBS_OVERVIEW_LIMIT, type JobsOverview } from '../useJobsOverview'
import { JobRow } from './JobRow'
import { JobsBackendStatus } from './JobsBackendStatus'
import { PollErrorBox } from './PollErrorBox'

const PAGE_SIZE = 20
/** 「進行中與失敗」模式：進行中、失敗各最多顯示幾筆（其餘請改用「顯示」篩選）。 */
const ATTENTION_SHOWN = 50

interface Props {
  jobs: JobsOverview
  /** 已載入的 repo 列表（App 持有）；用來把 repository_id 對應成名稱，沒有或找不到就顯示 #id。 */
  repos: Repository[] | null
  /** 對話框關閉（Esc、「關閉」鈕）後通知父元件卸載它。 */
  onClose: () => void
}

type Notice = { kind: 'success'; text: string } | { kind: 'error'; label: string; error: ApiError }

const KIND_OPTIONS = Object.keys(JOB_KIND_LABEL) as JobKind[]

/**
 * 「所有背景工作」完整列表（原生 <dialog>，showModal 開啟，由頂端摘要列的連結按需開啟；關閉時整個元件卸載，
 * 所以平常完全不載入列表資料）。鍵盤：焦點進入對話框並被限制在裡面，Esc 或「關閉」關閉，焦點回到開啟它的按鈕。
 * - 預設篩選「進行中與失敗」：直接用頁面共用的輪詢資料（useJobsOverview），不另外發請求；
 *   其他篩選才向後端分頁取一頁（useJobsList，開啟期間輪詢）。
 * - 已忽略的失敗工作仍列在這裡，標示「已忽略」，可以「取消忽略」。
 * 背景工作由後端的 worker 執行；這裡只讀取與送出取消／重試，不在前端重做狀態機。
 */
export function JobsDialog({ jobs, repos, onClose }: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [status, setStatus] = useState<string>(JOBS_ATTENTION) // 每次開啟都從「進行中與失敗」開始；'' 代表全部
  const [kind, setKind] = useState('')
  const [offset, setOffset] = useState(0)
  const [listReload, setListReload] = useState(0)
  const [acting, setActing] = useState<{ id: number; action: JobAction } | null>(null)
  const [notice, setNotice] = useState<Notice | null>(null)

  // 掛載時以 showModal 開啟（modal：背景不能操作、焦點被限制、Esc 會關閉）
  useEffect(() => {
    const dialog = dialogRef.current
    if (dialog !== null && !dialog.open) dialog.showModal()
  }, [])

  const attention = status === JOBS_ATTENTION
  const list = useJobsList(
    { status, kind, limit: PAGE_SIZE, offset },
    !attention,
    (jobs.inProgress ?? 0) > 0,
    `${listReload}:${jobs.status.data?.queued ?? 0}:${jobs.status.data?.running ?? 0}`,
  )

  // 一般模式：後端分頁的一頁。進行中與失敗模式：共用資料中進行中在前、失敗在後（未忽略的在前），不分頁
  let items: Job[] | null = null
  let loading = list.loading
  let attentionTruncated = false
  if (attention) {
    loading = jobs.activePoll.loading || jobs.failedPoll.loading
    if (jobs.activeJobs !== null && jobs.failedJobs !== null) {
      const matchesKind = (j: Job) => kind === '' || j.kind === kind
      const activeIds = new Set(jobs.activeJobs.map((j) => j.id))
      const active = jobs.activeJobs.filter(matchesKind)
      const failed = orderFailedForList(
        jobs.failedJobs.filter((j) => matchesKind(j) && !activeIds.has(j.id)),
        jobs.dismissed,
      )
      items = [...active.slice(0, ATTENTION_SHOWN), ...failed.slice(0, ATTENTION_SHOWN)]
      attentionTruncated =
        active.length > ATTENTION_SHOWN ||
        failed.length > ATTENTION_SHOWN ||
        (jobs.activePoll.data?.total ?? 0) > jobs.activeJobs.length ||
        (jobs.failedTotal ?? 0) > jobs.failedJobs.length
    }
  } else if (list.data !== null) {
    items = list.data.items
  }

  // 工作被刪除或篩選後筆數變少，目前這一頁可能已超出範圍：回到最後一頁（在 render 期間同步調整，不需要 effect）
  const listTotal = list.data?.total ?? 0
  if (!attention && items !== null && items.length === 0 && listTotal > 0 && offset > 0) {
    setOffset(Math.floor((listTotal - 1) / PAGE_SIZE) * PAGE_SIZE)
  }

  /** 標題用的短名稱（deds-portal）：對不到 repo 就顯示 #id；工作沒有 repo 時是 null。 */
  function repoShort(job: Job): string | null {
    if (job.repository_id === null) return null
    const repo = repos?.find((r) => r.id === job.repository_id)
    return repo ? repoTitle(repo) : `#${job.repository_id}`
  }

  /** 「詳情」裡顯示的 repo：#6 deds-portal。 */
  function repoFull(job: Job): string {
    if (job.repository_id === null) return '—'
    const repo = repos?.find((r) => r.id === job.repository_id)
    return repo ? repoLabel(repo) : `#${job.repository_id}`
  }

  function refresh() {
    jobs.reload()
    setListReload((k) => k + 1)
  }

  async function runAction(job: Job, action: JobAction) {
    if (acting !== null) return
    setActing({ id: job.id, action })
    setNotice(null)
    const label = JOB_ACTION_LABEL[action]
    const what = `「${jobTitle(job, repoShort(job))}」`
    try {
      const res = await api<Job>('POST', `/api/v1/jobs/${job.id}/${action}`)
      let text: string
      if (action === 'retry') {
        text = `${what}已重新排入，等待背景執行器處理。`
        jobs.restore(job.id) // 重新排入後若再失敗，要重新提醒
      } else if (res.status === 202) text = `已記錄${what}的取消請求（取消中…）；工作仍在執行，${cancellingWait(job.kind)}。`
      else text = `${what}已取消。`
      setNotice({ kind: 'success', text })
      refresh()
    } catch (e) {
      const error = toApiError(e)
      setNotice({ kind: 'error', label: `${label}${what}失敗`, error })
      // 404（已不存在）、409（狀態已變）：畫面上的資料已過期，立刻重抓
      if (error.status === 404 || error.status === 409) refresh()
    } finally {
      setActing(null)
    }
  }

  const hasFilter = kind !== '' || (status !== '' && !attention)
  const updatedAtMs = attention ? jobs.activePoll.updatedAt : list.updatedAt
  const updatedAt = updatedAtMs === null ? null : new Date(updatedAtMs).toLocaleTimeString('zh-TW', { hour12: false })
  const dismissedFailures = jobs.failedJobs?.filter((j) => jobs.dismissed.has(j.id)).length ?? 0

  return (
    <dialog ref={dialogRef} className="jobs-dialog" aria-labelledby="jobs-dialog-title" onClose={onClose}>
      <div className="jobs-dialog-head">
        <h2 id="jobs-dialog-title">所有背景工作</h2>
        <button type="button" className="secondary" onClick={() => dialogRef.current?.close()}>
          關閉
        </button>
      </div>

      <p className="note jobs-intro">
        這裡列出系統在背景幫你做的事（匯入版本、註冊 GitHub repo、更新 GitHub repo）。進度與結果平常已直接顯示在 repo
        卡片、詳情的版本區與「GitHub 註冊工作」區，不需要來這裡；工作卡住、想確認有沒有在跑，或想回頭看已忽略的失敗時再打開。
      </p>
      <p className="note">
        頂端與這裡的「失敗」＝<strong>尚未忽略</strong>的失敗工作；已忽略的（目前
        {jobs.failedJobs === null ? ' —' : ` ${dismissedFailures}`} 筆）仍列在下方並標示「已忽略」，可按「取消忽略」恢復提醒。
        「忽略」只記在這個瀏覽器，不會改變工作本身。
      </p>

      <section aria-labelledby="jobs-list-title">
        <h3 id="jobs-list-title">工作列表</h3>
        <ApiHint
          apis={[
            attention
              ? `GET /api/v1/jobs?status=active＋GET /api/v1/jobs?status=failed（各取最新 ${JOBS_OVERVIEW_LIMIT} 筆；與頁面頂端、repo 卡片共用同一份輪詢，這裡最多各顯示 ${ATTENTION_SHOWN} 筆）`
              : 'GET /api/v1/jobs?status=&kind=&limit=20&offset=（新的在前；只在這個篩選下才額外載入）',
          ]}
        />
        <div className="toolbar">
          <label>
            顯示
            <select
              value={status}
              onChange={(e) => {
                setStatus(e.target.value)
                setOffset(0)
              }}
            >
              {JOB_STATUS_FILTERS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            類型
            <select
              value={kind}
              onChange={(e) => {
                setKind(e.target.value)
                setOffset(0)
              }}
            >
              <option value="">全部</option>
              {KIND_OPTIONS.map((k) => (
                <option key={k} value={k}>
                  {JOB_KIND_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          <button type="button" className="secondary" onClick={refresh}>
            重新整理
          </button>
          {updatedAt !== null && <span className="muted">資料更新於 {updatedAt}（進行中每數秒自動更新）</span>}
        </div>

        {notice?.kind === 'success' && (
          <p className="success" role="status">
            {notice.text}
          </p>
        )}
        {notice?.kind === 'error' && (
          <div>
            <p className="note">{notice.label}：</p>
            <ErrorBox error={notice.error} />
          </div>
        )}
        <ApiHint
          apis={[
            'POST /api/v1/jobs/{id}/cancel（取消：排隊中立即取消 200；執行中回 202、cancel_requested＝true，status 仍是 running，worker 在批次之間停止；409＝已結束，404＝不存在）',
            'POST /api/v1/jobs/{id}/retry（重試失敗的工作）',
          ]}
        />

        {loading && <p role="status">載入中…</p>}
        {attention ? (
          <>
            <PollErrorBox state={jobs.activePoll} />
            <PollErrorBox state={jobs.failedPoll} />
          </>
        ) : (
          <PollErrorBox state={list} />
        )}
        {items !== null && items.length === 0 && (
          <p className="empty">
            {attention ? (
              <>
                目前沒有進行中或失敗的工作。{' '}
                <button type="button" className="linklike" onClick={() => setStatus('')}>
                  查看所有已完成的紀錄
                </button>
              </>
            ) : hasFilter ? (
              '沒有符合篩選條件的工作。'
            ) : (
              '目前沒有背景工作；匯入、GitHub 註冊與更新會以背景工作執行（背景執行器在線時）。'
            )}
          </p>
        )}
        {items !== null && items.length > 0 && (
          <>
            <ul className="job-list" aria-label="背景工作列表">
              {items.map((job) => {
                const dismissed = jobs.dismissed.has(job.id)
                return (
                  <JobRow
                    key={job.id}
                    job={job}
                    repoShort={repoShort(job)}
                    repoFull={repoFull(job)}
                    workerOffline={jobs.workerOffline}
                    acting={acting}
                    onAction={runAction}
                    dismissal={{ dismissed, onToggle: () => (dismissed ? jobs.restore(job.id) : jobs.dismiss(job.id)) }}
                  />
                )
              })}
            </ul>
            {attentionTruncated && (
              <p className="note">只列出最新的進行中與失敗工作（各最多 {ATTENTION_SHOWN} 筆）；其餘請改用上方「顯示」篩選查看。</p>
            )}
            {!attention && list.data !== null && (
              <Pager total={list.data.total} limit={list.data.limit} offset={list.data.offset} onChange={setOffset} />
            )}
          </>
        )}
      </section>

      <JobsBackendStatus status={jobs.status} />
    </dialog>
  )
}
