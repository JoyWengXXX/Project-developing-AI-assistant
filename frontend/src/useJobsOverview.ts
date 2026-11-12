import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { withQuery } from './api'
import { countOpenFailures, dismissedAndActive, groupJobsByRepo, NO_REPO_JOBS, type FailureCount, type RepoJobs } from './jobsOverview'
import type { Job, JobsStatus, Page } from './types'
import { useDismissedJobs } from './useDismissedJobs'
import { usePolling, type PollState } from './usePolling'

/** 有進行中工作時的輪詢間隔（規格 10.2 擬議 1.5 秒；總覽取 2.5 秒）。 */
export const JOBS_POLL_FAST_MS = 2_500
/** 沒有進行中工作時放慢，只為了發現「新出現的工作」與 worker 心跳變化。 */
export const JOBS_POLL_SLOW_MS = 15_000
/** 進行中、失敗各取最新的幾筆（後端單頁上限 200）；超過時失敗數會標示「N+」。 */
export const JOBS_OVERVIEW_LIMIT = 200

/** repo 卡片、詳情與完整列表要對工作做的事（忽略／取消忽略／立刻重新讀取）。 */
export interface JobControls {
  dismiss: (jobId: number) => void
  restore: (jobId: number) => void
  reload: () => void
}

export interface JobsOverview extends JobControls {
  /** GET /jobs/status：背景執行器是否在線、排隊與執行中的數量。 */
  status: PollState<JobsStatus>
  /** GET /jobs?status=active&limit=200 的輪詢狀態與項目（載入前為 null）。 */
  activePoll: PollState<Page<Job>>
  activeJobs: Job[] | null
  /** GET /jobs?status=failed&limit=200（含已忽略的；新的在前，由後端排序）。 */
  failedPoll: PollState<Page<Job>>
  failedJobs: Job[] | null
  failedTotal: number | null
  /** 排隊＋執行中（取自 /jobs/status）；還沒取得時為 null。 */
  inProgress: number | null
  /** 尚未忽略的失敗工作數；失敗列表還沒取得時為 null。 */
  openFailures: FailureCount | null
  /** 背景執行器（worker）不在線（尚未取得狀態時為 false，不誤報）。 */
  workerOffline: boolean
  dismissed: ReadonlySet<number>
  /** 某個 repo 的進行中工作與尚未忽略的失敗工作（沒有就是空的）。 */
  forRepo: (repoId: number) => RepoJobs
}

/**
 * 背景工作的「共用資料來源」：App 只在這裡輪詢一次，頂端摘要、repo 卡片、詳情版本區與完整列表共用同一份資料
 * （不要每張卡片各打一次請求）。每個輪詢週期的請求：
 *   - GET /jobs/status           有進行中（或剛送出匯入）2.5 秒；閒置 15 秒
 *   - GET /jobs?status=active    同上
 *   - GET /jobs?status=failed    15 秒；排隊／執行中的數量一變（有工作結束、失敗、被重試）就立刻重抓
 * 所以閒置時每 15 秒 3 個請求；匯入進行中約每 2.5 秒 2 個請求，另加每 15 秒 1 個。頁面隱藏時全部暫停，
 * 失敗時退避（usePolling 的規則）。「完整列表」開啟時不另外抓「進行中與失敗」：直接用這份資料。
 *
 * busyHint：呼叫端剛送出匯入、但 /jobs/status 還來不及反映時，先用較密的間隔。
 * 忽略（已存在 localStorage）的失敗工作不計入 openFailures，也不出現在 forRepo().failed。
 */
export function useJobsOverview(busyHint = false): JobsOverview {
  const dismissed = useDismissedJobs()
  const [reloadKey, setReloadKey] = useState(0)

  const status = usePolling<JobsStatus>('/api/v1/jobs/status', {
    enabled: true,
    reloadKey,
    intervalMs: (data) => (busyHint || (data !== null && data.queued + data.running > 0) ? JOBS_POLL_FAST_MS : JOBS_POLL_SLOW_MS),
  })
  const statusBusy = status.data !== null && status.data.queued + status.data.running > 0
  // 呼叫端剛開始匯入：現在排著的計時器可能是閒置時的 15 秒，立刻重抓一次，之後才會改用較密的間隔
  // （在 render 期間同步調整 state，與專案其他地方相同，不需要 effect）
  const [prevBusyHint, setPrevBusyHint] = useState(busyHint)
  if (busyHint !== prevBusyHint) {
    setPrevBusyHint(busyHint)
    if (busyHint) setReloadKey((k) => k + 1)
  }
  // 排隊／執行中的數量一變（有新工作、工作結束或失敗），兩份列表就立刻重抓，不必等下一個週期。
  // 還沒取得狀態時視為 0:0：第一次取得狀態（通常也是 0:0）不會讓剛載入的列表又重抓一次
  const listReloadKey = `${reloadKey}:${status.data?.queued ?? 0}:${status.data?.running ?? 0}`

  const activePoll = usePolling<Page<Job>>(withQuery('/api/v1/jobs', { status: 'active', limit: JOBS_OVERVIEW_LIMIT }), {
    enabled: true,
    reloadKey: listReloadKey,
    intervalMs: (data) => (busyHint || statusBusy || (data !== null && data.items.length > 0) ? JOBS_POLL_FAST_MS : JOBS_POLL_SLOW_MS),
  })
  const failedPoll = usePolling<Page<Job>>(withQuery('/api/v1/jobs', { status: 'failed', limit: JOBS_OVERVIEW_LIMIT }), {
    enabled: true,
    reloadKey: listReloadKey,
    intervalMs: () => JOBS_POLL_SLOW_MS,
  })

  const activeJobs = activePoll.data?.items ?? null
  const failedJobs = failedPoll.data?.items ?? null
  const failedTotal = failedPoll.data?.total ?? null

  // 已忽略的工作又被排入（重試、重新匯入同一個版本）：之後再失敗要重新提醒，所以取消忽略
  const { ids, restore } = dismissed
  useEffect(() => {
    if (activeJobs === null) return
    for (const id of dismissedAndActive(activeJobs, ids)) restore(id)
  }, [activeJobs, ids, restore])

  const byRepo = useMemo(() => groupJobsByRepo(activeJobs ?? [], failedJobs ?? [], ids), [activeJobs, failedJobs, ids])
  const forRepo = useCallback((repoId: number) => byRepo.get(repoId) ?? NO_REPO_JOBS, [byRepo])
  const openFailures = useMemo(
    () => (failedJobs === null || failedTotal === null ? null : countOpenFailures(failedJobs, failedTotal, ids, activeJobs ?? [])),
    [failedJobs, failedTotal, ids, activeJobs],
  )
  const reload = useCallback(() => setReloadKey((k) => k + 1), [])

  return {
    status,
    activePoll,
    activeJobs,
    failedPoll,
    failedJobs,
    failedTotal,
    inProgress: status.data === null ? null : status.data.queued + status.data.running,
    openFailures,
    workerOffline: status.data !== null && status.data.worker.status !== 'online',
    dismissed: ids,
    forRepo,
    dismiss: dismissed.dismiss,
    restore,
    reload,
  }
}

/**
 * flag 從 true 變成 false 的那一刻呼叫 callback（例如這個 repo 的進行中工作全部結束：重新讀取概況或快照）。
 * 第一次 render 不會呼叫；callback 用最新的那一個（不必穩定）。
 */
export function useOnFalling(flag: boolean, callback: () => void): void {
  const previous = useRef(flag)
  const latest = useRef(callback)
  useEffect(() => {
    latest.current = callback
  })
  useEffect(() => {
    if (previous.current && !flag) latest.current()
    previous.current = flag
  }, [flag])
}
