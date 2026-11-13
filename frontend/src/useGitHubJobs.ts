import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, isAbortError } from './api'
import { toApiError } from './format'
import { submitFetch, submitRegistration, type FetchSubmit, type RegisterSubmit } from './githubActions'
import { fetchResultOf, githubRepoOf, isGitHubJob, registerOutcome, registerResultOf, registrationJobs } from './githubJobs'
import type { FetchResult, Job } from './types'
import type { JobsOverview } from './useJobsOverview'
import { isFinished } from './versionJobs'

/** 註冊工作結束後留在「GitHub 註冊工作」區的一行說明（使用者關閉前保留）。 */
export interface RegisterNotice {
  jobId: number
  tone: 'success' | 'warning'
  text: string
}

const MAX_NOTICES = 5

export interface GitHubJobs {
  /** 進行中的註冊工作（含重新整理頁面前就開始的；註冊成功前沒有 repo，所以不在任何 repo 卡片上）。 */
  registerActive: Job[]
  /** 尚未忽略的註冊失敗（最近的在前）。 */
  registerFailed: Job[]
  notices: RegisterNotice[]
  dismissNotice: (jobId: number) => void
  /** 每個 repo 最近一次 fetch 的結果（refs 變動清單），下一次更新送出時清掉。 */
  fetchResults: Readonly<Record<number, FetchResult>>
  /** 每個 repo fetch 完成的次數：卡片與詳情的版本區據此重新讀取概況、分支與 commit 圖。 */
  fetchTicks: Readonly<Record<number, number>>
  /** 註冊。失敗（422、503…）會拋出 ApiError；onFallback 見 submitRegistration。 */
  register: (input: string, onFallback?: (reason: string) => void) => Promise<RegisterSubmit>
  /** 更新。失敗（409 repository_busy、503…）會拋出 ApiError。 */
  fetchRepo: (repoId: number, onFallback?: (reason: string) => void) => Promise<FetchSubmit>
}

/**
 * GitHub 註冊與更新的背景工作（App 持有一份）。
 *
 * 顯示用的資料全部來自頁面共用的輪詢（useJobsOverview，不另外發請求）：所以重新整理頁面後，進行中的註冊與更新仍看得到。
 * 這個 hook 額外做三件事：
 *   1. 送出：worker 在線送背景工作，否則回退同步端點（githubActions）。
 *   2. 偵測完成：記住「看過或送出過的」GitHub 工作編號；某個編號離開進行中清單時，補一次 GET /jobs/{id} 取得最終結果
 *      （規格 10.2）。成功才處理，且同一個工作編號只處理一次：
 *        - 註冊且 already_registered=false → 重新載入 repo 列表並自動匯入一次預設分支最新版本；
 *          already_registered=true（別人搶先註冊了）只重新載入列表，不匯入。
 *        - 更新 → 保存 refs 變動清單，並讓卡片與詳情重新讀取。
 *   3. 失敗與取消不在這裡處理：失敗的工作由共用輪詢的失敗清單顯示（重試、忽略）。
 *
 * onRegistered(repoId, created)：repo 已註冊（200 已存在、同步回退、背景工作成功）；created 為 true（新建）時由呼叫端自動匯入一次。
 */
export function useGitHubJobs(jobs: JobsOverview, onRegistered: (repoId: number, created: boolean) => void): GitHubJobs {
  const { activeJobs, failedJobs, dismissed, reload } = jobs
  const [fetchResults, setFetchResults] = useState<Record<number, FetchResult>>({})
  const [fetchTicks, setFetchTicks] = useState<Record<number, number>>({})
  const [notices, setNotices] = useState<RegisterNotice[]>([])
  const tracked = useRef(new Set<number>()) // 看過或送出過、還沒取得最終結果的 GitHub 工作
  const settling = useRef(new Set<number>()) // 正在補抓最終結果的工作（同一個工作不會同時抓兩次）
  const succeeded = useRef(new Set<number>()) // 成功結果已處理過的工作：自動匯入等動作只做一次
  const [lifetime] = useState(() => new AbortController()) // App 卸載時取消所有請求
  useEffect(() => () => lifetime.abort(), [lifetime])

  const registeredRef = useRef(onRegistered)
  useEffect(() => {
    registeredRef.current = onRegistered
  })

  const recordFetch = useCallback((repoId: number, result: FetchResult) => {
    setFetchResults((prev) => ({ ...prev, [repoId]: result }))
    setFetchTicks((prev) => ({ ...prev, [repoId]: (prev[repoId] ?? 0) + 1 }))
  }, [])

  const addNotice = useCallback((notice: RegisterNotice) => {
    setNotices((prev) => [notice, ...prev.filter((n) => n.jobId !== notice.jobId)].slice(0, MAX_NOTICES))
  }, [])

  // 工作已結束：成功的才有後續動作（同一個工作編號只處理一次）
  const finish = useCallback(
    (job: Job) => {
      reload() // 頂端的「進行中」「失敗」數量與卡片提示立刻更新
      if (job.status !== 'succeeded' || succeeded.current.has(job.id)) return
      succeeded.current.add(job.id)

      if (job.kind === 'github_register') {
        const outcome = registerOutcome(job)
        const name = registerResultOf(job)?.github_repo || githubRepoOf(job) || 'GitHub repo'
        if (outcome.kind === 'created') {
          registeredRef.current(outcome.repositoryId, true)
          addNotice({
            jobId: job.id,
            tone: 'success',
            text: `已註冊 ${name}（repo #${outcome.repositoryId}）：卡片已加入總覽，並自動匯入預設分支最新版本（只此一次，進度顯示在卡片上）。`,
          })
        } else if (outcome.kind === 'existing') {
          registeredRef.current(outcome.repositoryId, false)
          addNotice({ jobId: job.id, tone: 'success', text: `${name} 已經註冊過（repo #${outcome.repositoryId}），沒有重新複製，也不會匯入。` })
        } else {
          addNotice({ jobId: job.id, tone: 'warning', text: `註冊工作 #${job.id} 已完成，但後端沒有回報 repo 編號；請按「重新載入列表」確認。` })
        }
      } else if (job.kind === 'github_fetch') {
        const result = fetchResultOf(job)
        if (result !== null) recordFetch(job.repository_id ?? result.repository_id, result)
      }
    },
    [reload, addNotice, recordFetch],
  )

  // 補抓一個「離開進行中清單」的工作的最終結果；還沒結束（例如輪詢比剛送出的工作早一步）就保留，下次再看
  const settle = useCallback(
    async (jobId: number) => {
      if (settling.current.has(jobId)) return
      settling.current.add(jobId)
      try {
        const res = await api<Job>('GET', `/api/v1/jobs/${jobId}`, { signal: lifetime.signal })
        if (!isFinished(res.data)) return
        tracked.current.delete(jobId)
        finish(res.data)
      } catch (e) {
        if (isAbortError(e)) return
        if (toApiError(e).code === 'job_not_found') tracked.current.delete(jobId) // 已不存在（repo 被移除）：不再追蹤
        // 其他錯誤：保留，下一次輪詢再試
      } finally {
        settling.current.delete(jobId)
      }
    },
    [lifetime, finish],
  )

  // 共用輪詢每回來一次：看到的 GitHub 工作加入追蹤；追蹤中卻不在進行中清單的，補抓最終結果
  useEffect(() => {
    if (activeJobs === null) return
    const activeIds = new Set(activeJobs.map((j) => j.id))
    for (const job of activeJobs) if (isGitHubJob(job)) tracked.current.add(job.id)
    for (const id of tracked.current) if (!activeIds.has(id)) void settle(id)
  }, [activeJobs, settle])

  const register = useCallback(
    async (input: string, onFallback?: (reason: string) => void) => {
      const submit = await submitRegistration(input, { signal: lifetime.signal, onFallback })
      if (submit.kind === 'job') {
        tracked.current.add(submit.job.id)
        reload() // 立刻讓進度出現，不等下一個輪詢週期
      } else if (submit.kind === 'existing') {
        registeredRef.current(submit.repositoryId, false)
      } else {
        registeredRef.current(submit.repository.id, submit.created)
      }
      return submit
    },
    [lifetime, reload],
  )

  const fetchRepo = useCallback(
    async (repoId: number, onFallback?: (reason: string) => void) => {
      setFetchResults((prev) => {
        if (prev[repoId] === undefined) return prev
        const rest = { ...prev }
        delete rest[repoId]
        return rest
      })
      const submit = await submitFetch(repoId, { signal: lifetime.signal, onFallback })
      if (submit.kind === 'job') {
        tracked.current.add(submit.job.id)
        reload()
      } else {
        recordFetch(repoId, submit.result)
      }
      return submit
    },
    [lifetime, reload, recordFetch],
  )

  const registrations = useMemo(
    () => registrationJobs(activeJobs ?? [], failedJobs ?? [], dismissed),
    [activeJobs, failedJobs, dismissed],
  )
  const dismissNotice = useCallback((jobId: number) => setNotices((prev) => prev.filter((n) => n.jobId !== jobId)), [])

  return {
    registerActive: registrations.active,
    registerFailed: registrations.failed,
    notices,
    dismissNotice,
    fetchResults,
    fetchTicks,
    register,
    fetchRepo,
  }
}
