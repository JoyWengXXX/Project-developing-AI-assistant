import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError, isAbortError } from './api'
import { jobFailure, toApiError } from './format'
import { setReference } from './useReferences'
import type { Job, JobSubmitResponse, JobsStatus, Snapshot } from './types'
import { chooseLoadMode, isFinished, newerJob, type LoadMethod } from './versionJobs'

/**
 * 總覽卡片（與註冊後的一次性匯入）的「匯入一個版本」。
 * 與詳情頁（useVersionLoad）同一套決策：worker 與 dispatcher 皆 online 才走 POST /import-jobs＋輪詢 GET /jobs/{id}，
 * 否則回退到同步 POST /imports（chooseLoadMode，versionJobs.ts）。不同的是這裡一次只追蹤「每個 repo 一件事」，
 * 用簡單的 Record<repoId, ImportRun> 保存，不是全域佇列，也沒有自動重試或自動匯入。
 *
 * 狀態放在 App：註冊要花數秒到數分鐘，使用者可能已進入某個 repo 的詳情（總覽卸載），
 * 匯入仍會繼續，回到總覽時卡片接著顯示結果。
 */

export interface ImportRun {
  /** 要匯入的版本：完整 SHA，或註冊後自動匯入用的 "HEAD"。 */
  revision: string
  /** 匯入成功後自動把這個版本設為參考版本（「匯入並改用最新版本」）。匯入失敗就不會設定。 */
  thenReference: boolean
  status: 'running' | 'failed' | 'done'
  job: Job | null
  method: LoadMethod | null
  /** 輪詢 job 失敗（網路等）：畫面上的進度可能過期，會自動退避重試。 */
  pollError: ApiError | null
  error: ApiError | null
  /** 失敗發生在哪一步：匯入本身，或匯入成功後設為參考。 */
  errorStep: 'import' | 'reference' | null
}

export interface RepoImports {
  runs: Readonly<Record<number, ImportRun>>
  /** 每個 repo 匯入完成的次數：卡片據此重新讀取概況與已匯入版本。 */
  doneTicks: Readonly<Record<number, number>>
  /** 任何 repo 有匯入進行中（使用者按的「匯入」一次只做一個；註冊後的自動匯入不受此限）。 */
  busy: boolean
  start: (repoId: number, revision: string, options?: { thenReference?: boolean }) => void
  /** 清除某個 repo 的結果（失敗訊息按「關閉」）。 */
  dismiss: (repoId: number) => void
  /** 匯入完成且卡片重新讀取概況後：清掉紀錄，或（概況仍顯示尚未匯入時）改成失敗狀態。 */
  settle: (repoId: number, reflected: boolean) => void
}

const POLL_MS = 1_500
const BACKOFF_BASE_MS = 5_000
const BACKOFF_MAX_MS = 60_000
const NOT_REFLECTED_ERROR = new ApiError(
  null,
  'not_analyzed_after_import',
  '匯入已完成，但概況仍顯示尚未匯入預設分支最新版本（匯入設定可能不一致，或預設分支剛又前進）。',
  null,
)

/** 可被中止的 setTimeout：中止時以 AbortError 結束。 */
function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(new DOMException('Aborted', 'AbortError'))
    const timer = window.setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      window.clearTimeout(timer)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

function withoutKey<T>(record: Record<number, T>, key: number): Record<number, T> {
  const copy = { ...record }
  delete copy[key]
  return copy
}

export function useRepoImports(): RepoImports {
  const [runs, setRuns] = useState<Record<number, ImportRun>>({})
  const [doneTicks, setDoneTicks] = useState<Record<number, number>>({})
  const active = useRef(new Set<number>()) // 同步可讀：同一個 repo 不會同時有兩個匯入
  const [lifetime] = useState(() => new AbortController()) // App 卸載時取消所有請求與輪詢
  useEffect(() => () => lifetime.abort(), [lifetime])

  const patch = useCallback((repoId: number, changes: Partial<ImportRun>) => {
    setRuns((prev) => (prev[repoId] === undefined ? prev : { ...prev, [repoId]: { ...prev[repoId], ...changes } }))
  }, [])

  /** 取得匯入的快照編號：背景工作（含輪詢）或同步回退。 */
  const importSnapshot = useCallback(
    async (repoId: number, revision: string): Promise<number> => {
      const signal = lifetime.signal
      // 送出當下才查一次 worker 狀態（不另外輪詢）；查不到就視為不可用，回退同步
      let statusSnapshot: { data: JobsStatus | null; error: unknown; loading: boolean }
      try {
        const res = await api<JobsStatus>('GET', '/api/v1/jobs/status', { signal })
        statusSnapshot = { data: res.data, error: null, loading: false }
      } catch (e) {
        if (isAbortError(e)) throw e
        statusSnapshot = { data: null, error: e, loading: false }
      }
      const decision = chooseLoadMode(statusSnapshot)

      if (decision.mode !== 'job') {
        const reason = decision.mode === 'sync' ? decision.reason : '改用同步匯入'
        patch(repoId, { method: { kind: 'sync', reason } })
        const res = await api<Snapshot>('POST', `/api/v1/repositories/${repoId}/imports`, {
          body: { revision },
          signal,
        })
        return res.data.id
      }

      patch(repoId, { method: { kind: 'job' } })
      const res = await api<JobSubmitResponse>('POST', `/api/v1/repositories/${repoId}/import-jobs`, {
        body: { revision },
        signal,
      })
      const { job: submitted, snapshot_id: snapshotId, reused } = res.data
      if (reused && snapshotId !== null) {
        patch(repoId, { method: { kind: 'reused' } }) // 已存在，沒有建立 job
        return snapshotId
      }
      if (submitted === null) throw new ApiError(res.status, 'invalid_response', '後端沒有回傳背景工作。', res.requestId)

      let job = submitted
      patch(repoId, { job })
      let failures = 0
      while (!isFinished(job)) {
        await sleep(failures === 0 ? POLL_MS : Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** (failures - 1)), signal)
        try {
          const polled = await api<Job>('GET', `/api/v1/jobs/${job.id}`, { signal })
          failures = 0
          job = newerJob(job, polled.data)
          patch(repoId, { job, pollError: null })
        } catch (e) {
          if (isAbortError(e)) throw e
          const error = toApiError(e)
          if (error.code === 'job_not_found') throw error // 工作已不存在（例如 repo 被移除）：不再輪詢
          failures++
          patch(repoId, { pollError: error }) // 暫時性失敗：保留最後的進度，退避後再試
        }
      }
      if (job.status !== 'succeeded') throw jobFailure(job)
      if (job.snapshot_id === null) {
        throw new ApiError(null, 'invalid_response', '背景工作已成功，但沒有回報快照編號。', job.request_id)
      }
      return job.snapshot_id
    },
    [lifetime, patch],
  )

  const start = useCallback(
    (repoId: number, revision: string, options?: { thenReference?: boolean }) => {
      if (active.current.has(repoId)) return // 同一個 repo 已在匯入：重複點擊只會送出一次
      active.current.add(repoId)
      const thenReference = options?.thenReference ?? false
      setRuns((prev) => ({
        ...prev,
        [repoId]: {
          revision,
          thenReference,
          status: 'running',
          job: null,
          method: null,
          pollError: null,
          error: null,
          errorStep: null,
        },
      }))

      void (async () => {
        let step: 'import' | 'reference' = 'import'
        try {
          const snapshotId = await importSnapshot(repoId, revision)
          if (thenReference) {
            step = 'reference' // 匯入成功才設定，失敗不會留下半套狀態
            await setReference(repoId, snapshotId)
          }
          if (lifetime.signal.aborted) return
          patch(repoId, { status: 'done', pollError: null })
          setDoneTicks((prev) => ({ ...prev, [repoId]: (prev[repoId] ?? 0) + 1 }))
        } catch (e) {
          if (isAbortError(e) || lifetime.signal.aborted) return
          patch(repoId, { status: 'failed', error: toApiError(e), errorStep: step, pollError: null })
        } finally {
          active.current.delete(repoId)
        }
      })()
    },
    [importSnapshot, lifetime, patch],
  )

  const dismiss = useCallback((repoId: number) => {
    setRuns((prev) => (prev[repoId]?.status === 'running' ? prev : withoutKey(prev, repoId)))
  }, [])

  /**
   * 匯入完成、卡片重新讀取概況之後呼叫：reflected＝概況已反映新版本（清掉紀錄）；
   * 否則改成失敗狀態（匯入說成功了，概況卻仍顯示尚未匯入），讓使用者看到原因並自行決定要不要重試。
   */
  const settle = useCallback((repoId: number, reflected: boolean) => {
    setRuns((prev) => {
      const run = prev[repoId]
      if (run === undefined || run.status !== 'done') return prev
      if (reflected) return withoutKey(prev, repoId)
      return { ...prev, [repoId]: { ...run, status: 'failed', error: NOT_REFLECTED_ERROR, errorStep: 'import' } }
    })
  }, [])

  const busy = Object.values(runs).some((r) => r.status === 'running')
  return { runs, doneTicks, busy, start, dismiss, settle }
}
