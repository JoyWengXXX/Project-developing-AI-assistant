// 背景工作「總覽」用的純邏輯（不含 React，方便單獨驗證）：
//   - 把進行中／失敗的工作依 repo 分組，供 repo 卡片與詳情版本區使用；
//   - 失敗工作是否已被之後成功的匯入取代；
//   - 頂端「失敗 N」的計數與摘要文字。
// 注意：這個檔案只匯入型別（不匯入其他模組的值），讓暫時的 Node 腳本可以直接載入它做驗證。

import type { Job, SnapshotItem } from './types'

/** 一個 repo 的背景工作：進行中（queued／running，新的在前）與「尚未忽略」的失敗（最近失敗的在前）。 */
export interface RepoJobs {
  active: Job[]
  failed: Job[]
}

export const NO_REPO_JOBS: RepoJobs = { active: [], failed: [] }

/** params 中的完整 commit SHA（小寫）；params 是不可信內容，格式不符就當作沒有。 */
export function jobCommitSha(job: Job): string | null {
  const value = job.params?.commit_sha
  return typeof value === 'string' && /^[0-9a-f]{7,64}$/i.test(value) ? value.toLowerCase() : null
}

/** 失敗發生的時間（毫秒）：優先用結束時間；無法解析時回傳 0。 */
export function failedAt(job: Job): number {
  const t = Date.parse(job.finished_at ?? job.updated_at)
  return Number.isNaN(t) ? 0 : t
}

/** 最近失敗的在前；同一時間以編號較大的（較新建立的）在前。 */
function newestFailureFirst(a: Job, b: Job): number {
  return failedAt(b) - failedAt(a) || b.id - a.id
}

function isActive(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/**
 * 依 repository_id 分組。沒有 repo 的工作（例如註冊 GitHub repo 失敗）不屬於任何卡片，只出現在頂端計數與完整列表。
 * failed 只留「尚未忽略」的；同一個編號若同時出現在 active（兩個請求之間剛好被重試），以 active 為準。
 */
export function groupJobsByRepo(
  active: readonly Job[],
  failed: readonly Job[],
  dismissed: ReadonlySet<number>,
): Map<number, RepoJobs> {
  const map = new Map<number, RepoJobs>()
  const entry = (repoId: number): RepoJobs => {
    let value = map.get(repoId)
    if (value === undefined) {
      value = { active: [], failed: [] }
      map.set(repoId, value)
    }
    return value
  }
  const activeJobs = active.filter(isActive)
  const activeIds = new Set(activeJobs.map((j) => j.id))
  for (const job of [...activeJobs].sort((a, b) => b.id - a.id)) {
    if (job.repository_id !== null) entry(job.repository_id).active.push(job)
  }
  for (const job of [...failed].sort(newestFailureFirst)) {
    if (job.status !== 'failed' || job.repository_id === null) continue
    if (dismissed.has(job.id) || activeIds.has(job.id)) continue
    entry(job.repository_id).failed.push(job)
  }
  return map
}

export interface FailureCount {
  count: number
  /** 失敗工作比一次取得的筆數多：超出的部分無法知道是否已忽略，一律算「未忽略」，所以數字可能偏高。 */
  approximate: boolean
}

/**
 * 頂端「失敗 N」＝尚未忽略的失敗工作數。
 * failed 是 GET /jobs?status=failed&limit=200 的項目，total 是後端回報的失敗總數（可能大於 failed 的筆數）。
 */
export function countOpenFailures(
  failed: readonly Job[],
  total: number,
  dismissed: ReadonlySet<number>,
  active: readonly Job[] = [],
): FailureCount {
  const activeIds = new Set(active.map((j) => j.id))
  const shown = failed.filter((j) => !dismissed.has(j.id) && !activeIds.has(j.id)).length
  const hidden = Math.max(0, total - failed.length)
  return { count: shown + hidden, approximate: hidden > 0 }
}

/**
 * 這個失敗之後，是否已有更新的成功匯入（同一個 repo 在失敗之後完成了任何一份快照）。
 * 為什麼用快照而不是 job：成功匯入的 job 不在「進行中／失敗」的輪詢裡，但卡片與版本區本來就載入了
 * 這個 repo 的快照列表（含 completed_at），不需要多一個請求。同一個版本重試成功時，同一個 job 會離開失敗清單，
 * 所以這裡處理的是「別的版本後來匯入成功了，舊的失敗已經不是最新狀況」。
 */
export function isSuperseded(
  job: Job,
  snapshots: readonly Pick<SnapshotItem, 'status' | 'completed_at'>[],
): boolean {
  const failedTime = failedAt(job)
  if (failedTime === 0) return false
  return snapshots.some(
    (s) => s.status === 'completed' && s.completed_at !== null && Date.parse(s.completed_at) > failedTime,
  )
}

/** 完整列表「進行中與失敗」模式的失敗工作排序：未忽略的在前，已忽略的在後；各自最近失敗的在前。 */
export function orderFailedForList(failed: readonly Job[], dismissed: ReadonlySet<number>): Job[] {
  const sorted = [...failed].sort(newestFailureFirst)
  return [...sorted.filter((j) => !dismissed.has(j.id)), ...sorted.filter((j) => dismissed.has(j.id))]
}

/** 已忽略、但又出現在「進行中」的工作（被重試或重新排入）：之後若再失敗，應該重新提醒，所以要取消忽略。 */
export function dismissedAndActive(active: readonly Job[], dismissed: ReadonlySet<number>): number[] {
  return active.filter((j) => isActive(j) && dismissed.has(j.id)).map((j) => j.id)
}

export type SummaryInput =
  | { kind: 'checking' }
  | { kind: 'unavailable' } // 取不到 /jobs/status
  | {
      kind: 'ready'
      inProgress: number
      /** 失敗工作數；失敗列表取不到時為 null（顯示「?」，不假裝是 0）。 */
      failed: FailureCount | null
      workerOnline: boolean
    }

/** 頂端摘要的文字（給螢幕閱讀器的 live 區用；畫面上的徽章與它同義）。 */
export function jobsSummaryText(input: SummaryInput): string {
  if (input.kind === 'checking') return '背景工作：檢查中'
  if (input.kind === 'unavailable') return '背景工作：無法取得狀態'
  const worker = input.workerOnline ? '背景執行器在線' : '背景執行器離線'
  const failedCount = input.failed?.count ?? 0
  if (input.inProgress === 0 && input.failed !== null && failedCount === 0) return `背景工作：無 · ${worker}`
  const failed = input.failed === null ? '?' : `${failedCount}${input.failed.approximate ? '+' : ''}`
  return `進行中 ${input.inProgress} · 失敗 ${failed} · ${worker}`
}
