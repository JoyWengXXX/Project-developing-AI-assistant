import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, ApiError, isAbortError } from './api'
import { jobFailure, toApiError } from './format'
import type { Job, JobSubmitResponse, JobsStatus, Snapshot } from './types'
import { usePolling, type PollState } from './usePolling'
import { chooseLoadMode, isFinished, isJobActive, newerJob, type LoadMethod, type LoadModeDecision } from './versionJobs'

export interface VersionLoad {
  /** idle＝沒有選取，或選取了但還沒按「匯入此版本」（不會自動匯入）。 */
  status: 'idle' | 'loading' | 'ready' | 'error'
  snapshot: Snapshot | null
  /** 載入失敗：請求本身的錯誤（409／422／503…），或背景工作 failed／cancelled 的 error。 */
  error: ApiError | null
  /** 目前選取的 commit 的背景工作（沒有用背景工作、或還沒提交時為 null）。 */
  job: Job | null
  /** 輪詢這個 job 失敗（網路等）：畫面上的進度可能過期，輪詢會自動退避重試。 */
  pollError: ApiError | null
  /** 目前選取的 commit 用哪種方式載入（還沒決定時為 null）。 */
  method: LoadMethod | null
  /** GET /jobs/status 最近一次成功的資料（取不到時為 null），用來提示「worker 不在線」。 */
  jobsStatus: JobsStatus | null
  /** 本次瀏覽中已確認有快照的 commit SHA（用來即時把「已匯入」標記更新為 true）。 */
  loadedShas: ReadonlySet<string>
  /** 正在載入的 commit SHA（請求進行中，或目前選取的 commit 的背景工作進行中）。 */
  loadingShas: ReadonlySet<string>
  /** 目前會用哪種方式匯入（按下「匯入此版本」的那一刻才真正決定；wait＝還不知道 worker 是否在線）。 */
  decision: LoadModeDecision
  /** 使用者按「匯入此版本」：提交目前選取的 commit（同一個 SHA 進行中、或已匯入時不會重複送出）。 */
  start: () => void
  /** 失敗後重試目前的版本（重新 POST；後端會把失敗的 job 重新排入）。 */
  retry: () => void
  /** 這個版本已被移除：忘掉本次瀏覽中記住的快照與 job（之後再按「匯入此版本」會重新匯入）。 */
  forget: (sha: string) => void
}

interface Failure {
  sha: string
  error: ApiError
}

/** 輪詢 job 的間隔（規格 10.2 擬議 1.5 秒；頁面隱藏時由 usePolling 暫停）。 */
const JOB_POLL_MS = 1_500

/** 頁面共用的 /jobs/status 輪詢（useJobsOverview）：決定匯入方式用，不在這裡另外輪詢。 */
export interface JobsStatusSource {
  status: PollState<JobsStatus>
  /** 剛送出了新的工作：立刻重抓，讓輪詢轉成較密的間隔。 */
  reload: () => void
}

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const copy = { ...record }
  delete copy[key]
  return copy
}

/**
 * 把「選取的 commit」匯入成快照——**只在使用者按「匯入此版本」（start）時才做，選取 commit 不會自動匯入**。
 * 優先用背景工作，worker 不可用時回退到同步匯入：
 *
 *   背景工作（worker 與 dispatcher 皆 online）：
 *     POST /import-jobs（revision＝完整 SHA）
 *       - 200 reused：快照早已存在，直接 GET /snapshots/{id}，不輪詢；
 *       - 202：記住 SHA→jobId，輪詢 GET /jobs/{id}（1.5 秒），succeeded 後以 snapshot_id 取得快照。
 *   回退（worker 或 dispatcher 不是 online，或 GET /jobs/status 取不到）：
 *     POST /imports（已棄用的同步端點；請求會占住直到匯入完成）。
 *   「用哪一種」在送出的那一刻決定，依據的是頁面共用的 /jobs/status 輪詢（jobsSource 參數，不另外多一次請求）。
 *
 * 規則：
 *   - 同一個 SHA 的請求只會有一個在進行（inflight）；進行中（請求中或 job 排隊／執行中）再按 start 不會重複提交。
 *   - 切換 commit 時不取消後端的 job（取消 UI 屬切片 4），只是不再輪詢它、不顯示它的結果。
 *     回到該 commit 時，用「SHA→jobId」的記憶接續輪詢同一個 job，不重複提交。
 *     限制：記憶只存在這個元件的生命週期（換 repo、離開詳情頁就沒了；之後回來要再按「匯入此版本」，
 *     由後端的 dedupe 回既有進行中的 job 或快照，所以不會重複工作）。
 *   - 失敗的 job 會一直顯示失敗（不因為切走再回來而自動重送），按「重試匯入」才重新 POST；
 *     請求本身的錯誤（409／422／503…）切換 commit 後就清掉（回到未匯入，可再按一次）。
 *   - 元件卸載（換 repo、repo 被移除）時取消進行中的請求與輪詢。
 *   - 「已匯入」的判斷（含需要快照編號才能設為參考或移除）在 VersionPanel：commits 的 imported 欄位＋本次匯入成功的狀態，
 *     快照編號取自 GET /repositories/{id}/snapshots（唯讀）；這裡不為了取得編號而再送匯入請求。
 * onLoaded：每次「新取得」快照時呼叫一次；created 為 true 表示本次由匯入產生（同步 201 或背景工作完成），
 * false 為既有（同步 200 或 import-jobs 200 reused）。
 */
export function useVersionLoad(
  repoId: number,
  sha: string | null,
  onLoaded: (snapshot: Snapshot, created: boolean) => void,
  jobsSource: JobsStatusSource,
): VersionLoad {
  const [loaded, setLoaded] = useState<Record<string, Snapshot>>({})
  const [failure, setFailure] = useState<Failure | null>(null)
  const [jobIds, setJobIds] = useState<Record<string, number>>({}) // SHA → 這個 SHA 提交過的 jobId
  const [jobs, setJobs] = useState<Record<number, Job>>({}) // jobId → 最新（較新）的 job 資料
  const [methods, setMethods] = useState<Record<string, LoadMethod>>({})
  const [busy, setBusy] = useState<Record<string, true>>({}) // 請求進行中的 SHA（POST、同步匯入、取快照）
  const inflight = useRef(new Set<string>()) // 同上，但同步可讀（避免同一個 SHA 重複送出）
  const [lifetime] = useState(() => new AbortController()) // 元件卸載時取消所有進行中的請求
  useEffect(() => () => lifetime.abort(), [lifetime])
  // 請求建立之後才完成的回呼要呼叫「最新的」onLoaded，所以放進 ref
  const onLoadedRef = useRef(onLoaded)
  useEffect(() => {
    onLoadedRef.current = onLoaded
  })

  // worker 是否在線：用頁面共用的 /jobs/status 輪詢（有排隊或執行中的工作時較密）
  const { status: jobsStatus, reload: reloadJobsStatus } = jobsSource
  const decision = chooseLoadMode(jobsStatus)

  // 換了要檢視的 commit：清掉上一個 commit 的請求錯誤（render 期間調整 state，不需要 effect）
  const [prevSha, setPrevSha] = useState(sha)
  if (sha !== prevSha) {
    setPrevSha(sha)
    setFailure(null)
  }

  const snapshot = sha !== null ? (loaded[sha] ?? null) : null
  const jobId = sha !== null ? jobIds[sha] : undefined
  const job = jobId !== undefined ? (jobs[jobId] ?? null) : null

  // 輪詢目前選取的 commit 的 job；快照已取得或 job 已結束就停止（最後一份資料保存在 jobs）
  const polling = jobId !== undefined && snapshot === null && !(job !== null && isFinished(job))
  const poll = usePolling<Job>(jobId !== undefined ? `/api/v1/jobs/${jobId}` : null, {
    enabled: polling,
    intervalMs: () => JOB_POLL_MS,
  })

  // 輪詢到的資料併入 jobs（render 期間調整 state；usePolling 已丟棄過期回應，這裡再以 updated_at 把關）
  const [seenPoll, setSeenPoll] = useState<Job | null>(null)
  const polled = poll.data
  if (polled !== null && polled !== seenPoll) {
    setSeenPoll(polled)
    setJobs((prev) => ({ ...prev, [polled.id]: newerJob(prev[polled.id], polled) }))
  }
  // job 已不存在（404 job_not_found，例如 repo 被移除）：忘掉它，顯示錯誤，讓使用者重試
  const pollError = poll.error
  if (sha !== null && jobId !== undefined && pollError !== null && pollError.code === 'job_not_found') {
    setJobIds((prev) => without(prev, sha))
    setFailure({ sha, error: pollError })
  }

  // ----- 送出與取得快照 -----

  /** 同一個 SHA 同時只做一件事；錯誤記成 failure（只有目前選取的 SHA 才會顯示）。 */
  async function run(forSha: string, work: () => Promise<void>) {
    if (inflight.current.has(forSha)) return
    inflight.current.add(forSha)
    setBusy((prev) => ({ ...prev, [forSha]: true }))
    try {
      await work()
    } catch (e) {
      if (!isAbortError(e)) setFailure({ sha: forSha, error: toApiError(e) })
    } finally {
      inflight.current.delete(forSha)
      if (!lifetime.signal.aborted) setBusy((prev) => without(prev, forSha))
    }
  }

  async function loadSnapshot(forSha: string, snapshotId: number, created: boolean) {
    const res = await api<Snapshot>('GET', `/api/v1/snapshots/${snapshotId}`, { signal: lifetime.signal })
    if (lifetime.signal.aborted) return
    setLoaded((prev) => ({ ...prev, [forSha]: res.data }))
    onLoadedRef.current(res.data, created)
  }

  async function submitJob(forSha: string) {
    setMethods((prev) => ({ ...prev, [forSha]: { kind: 'job' } }))
    const res = await api<JobSubmitResponse>('POST', `/api/v1/repositories/${repoId}/import-jobs`, {
      body: { revision: forSha },
      signal: lifetime.signal,
    })
    if (lifetime.signal.aborted) return
    const { job: submitted, snapshot_id: snapshotId, reused } = res.data
    if (reused && snapshotId !== null) {
      setMethods((prev) => ({ ...prev, [forSha]: { kind: 'reused' } }))
      await loadSnapshot(forSha, snapshotId, false)
      return
    }
    if (submitted === null) {
      throw new ApiError(res.status, 'invalid_response', '後端沒有回傳背景工作。', res.requestId)
    }
    setJobs((prev) => ({ ...prev, [submitted.id]: newerJob(prev[submitted.id], submitted) }))
    setJobIds((prev) => ({ ...prev, [forSha]: submitted.id })) // 之後由上面的輪詢接手
    reloadJobsStatus() // 有新的工作了：立刻重抓 worker 狀態（也讓輪詢轉成較密的間隔）
  }

  async function syncImport(forSha: string, reason: string) {
    setMethods((prev) => ({ ...prev, [forSha]: { kind: 'sync', reason } }))
    const res = await api<Snapshot>('POST', `/api/v1/repositories/${repoId}/imports`, {
      body: { revision: forSha },
      signal: lifetime.signal,
    })
    if (lifetime.signal.aborted) return
    setLoaded((prev) => ({ ...prev, [forSha]: res.data }))
    onLoadedRef.current(res.data, res.status === 201)
  }

  // job 成功：以 snapshot_id 取得快照
  const succeededSnapshotId = job !== null && job.status === 'succeeded' ? job.snapshot_id : null
  const hasFailure = failure !== null && failure.sha === sha
  const latest = useRef({ run, loadSnapshot })
  useEffect(() => {
    latest.current = { run, loadSnapshot }
  })
  useEffect(() => {
    if (sha === null || snapshot !== null || succeededSnapshotId === null || hasFailure) return
    const { run: runNow, loadSnapshot: fetchSnapshot } = latest.current
    void runNow(sha, () => fetchSnapshot(sha, succeededSnapshotId, true))
  }, [sha, snapshot, succeededSnapshotId, hasFailure, repoId, lifetime])

  // ----- 對外的狀態 -----

  const loadedShas = useMemo(() => new Set(Object.keys(loaded)), [loaded])
  const loadingShas = useMemo(() => {
    const shas = new Set(Object.keys(busy))
    if (sha !== null && snapshot === null && isJobActive(job ?? undefined)) shas.add(sha)
    return shas
  }, [busy, sha, snapshot, job])

  let error: ApiError | null = null
  if (failure !== null && failure.sha === sha) error = failure.error
  else if (job !== null && (job.status === 'failed' || job.status === 'cancelled')) error = jobFailure(job)
  else if (job !== null && job.status === 'succeeded' && job.snapshot_id === null) {
    error = new ApiError(null, 'invalid_response', '背景工作已成功，但沒有回報快照編號。', job.request_id)
  }
  const active = sha !== null && (loadingShas.has(sha) || (job !== null && job.status === 'succeeded'))
  const status: VersionLoad['status'] =
    sha === null ? 'idle' : snapshot !== null ? 'ready' : error !== null ? 'error' : active ? 'loading' : 'idle'

  /** 送出匯入（背景工作或同步回退）；不檢查是否已在進行，呼叫端負責（start／retry）。 */
  function submit() {
    if (sha === null || decision.mode === 'wait') return // wait：還不知道 worker 是否在線，按鈕此時是停用的
    const mode = decision
    void run(sha, () => (mode.mode === 'job' ? submitJob(sha) : syncImport(sha, mode.reason)))
  }

  function start() {
    if (sha === null || snapshot !== null || status === 'loading') return // 已匯入或進行中：不重複送出
    setFailure(null)
    submit()
  }

  function retry() {
    setFailure(null)
    // 忘掉失敗的 job 再重新 POST（後端把失敗的同鍵 job 重新排入，或回既有進行中的 job／快照）
    if (sha !== null) setJobIds((prev) => without(prev, sha))
    submit()
  }

  // 穩定的函式（只用 setState）：VersionPanel 把它放進 effect 的依賴列
  const forget = useCallback((forSha: string) => {
    setLoaded((prev) => without(prev, forSha))
    setJobIds((prev) => without(prev, forSha))
    setMethods((prev) => without(prev, forSha))
    setFailure((prev) => (prev !== null && prev.sha === forSha ? null : prev))
  }, [])

  return {
    status,
    snapshot,
    error,
    job,
    pollError: polling && pollError !== null && pollError.code !== 'job_not_found' ? pollError : null,
    method: sha !== null ? (methods[sha] ?? null) : null,
    jobsStatus: jobsStatus.error === null ? jobsStatus.data : null,
    loadedShas,
    loadingShas,
    decision,
    start,
    retry,
    forget,
  }
}
