// GitHub 註冊（github_register）與更新（github_fetch）背景工作的純邏輯（不含 React，方便單獨驗證）：
//   - 讀取 job.params／job.result（不可信內容：每個欄位都先檢查型別，不符就當作沒有）；
//   - 註冊工作的結果分類（新建、已存在、失敗…），決定要不要自動匯入；
//   - 把共用輪詢的進行中／失敗工作分出「註冊」與「某個 repo 的更新」。

import { failedAt, type RepoJobs } from './jobsOverview'
import type { FetchResult, Job, RefChange, RegisterJobResult } from './types'

export function isGitHubJob(job: Job): boolean {
  return job.kind === 'github_register' || job.kind === 'github_fetch'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function positiveInt(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : null
}

/** params.github_repo（註冊工作的 owner/name；後端已驗證過）。只當文字顯示，過長就截斷。 */
export function githubRepoOf(job: Job): string | null {
  const value = job.params?.github_repo
  if (typeof value !== 'string' || value === '') return null
  return value.length > 200 ? `${value.slice(0, 200)}…` : value
}

/** 註冊成功的 result；欄位缺少或型別不符回傳 null。repository_id 缺少時退回 job.repository_id。 */
export function registerResultOf(job: Job): RegisterJobResult | null {
  const result = job.result
  if (!isRecord(result)) return null
  const repositoryId = positiveInt(result.repository_id) ?? job.repository_id
  if (repositoryId === null || typeof result.already_registered !== 'boolean') return null
  return {
    repository_id: repositoryId,
    github_repo: typeof result.github_repo === 'string' ? result.github_repo : (githubRepoOf(job) ?? ''),
    already_registered: result.already_registered,
  }
}

export type RegisterOutcome =
  | { kind: 'pending' } // 排隊或執行中
  | { kind: 'created'; repositoryId: number } // 成功且是這個工作新建的 repo：要自動匯入一次
  | { kind: 'existing'; repositoryId: number } // 成功，但 repo 早已註冊（例如同步端點搶先）：不匯入
  | { kind: 'failed' }
  | { kind: 'cancelled' }
  | { kind: 'invalid' } // 後端說成功了，result 卻讀不出 repo 編號

/** 註冊工作的結果分類。成功才有 repo；already_registered=false 才是「新註冊」（自動匯入只在這時觸發）。 */
export function registerOutcome(job: Job): RegisterOutcome {
  switch (job.status) {
    case 'queued':
    case 'running':
      return { kind: 'pending' }
    case 'failed':
      return { kind: 'failed' }
    case 'cancelled':
      return { kind: 'cancelled' }
    case 'succeeded': {
      const result = registerResultOf(job)
      if (result === null) return { kind: 'invalid' }
      return result.already_registered
        ? { kind: 'existing', repositoryId: result.repository_id }
        : { kind: 'created', repositoryId: result.repository_id }
    }
  }
}

const CHANGES: readonly string[] = ['added', 'updated', 'removed']
const KINDS: readonly string[] = ['branch', 'tag']

function refChangeOf(value: unknown): RefChange | null {
  if (!isRecord(value)) return null
  const { name, kind, change, old_sha: oldSha, new_sha: newSha } = value
  if (typeof name !== 'string' || typeof kind !== 'string' || typeof change !== 'string') return null
  if (!KINDS.includes(kind) || !CHANGES.includes(change)) return null
  if ((oldSha !== null && typeof oldSha !== 'string') || (newSha !== null && typeof newSha !== 'string')) return null
  return { name, kind: kind as RefChange['kind'], change: change as RefChange['change'], old_sha: oldSha, new_sha: newSha }
}

/** 更新成功的 result（形狀與同步 fetch 的回應一致，另有 changed_refs）；讀不出 repo 編號回傳 null。格式不對的 ref 項目會被略過。 */
export function fetchResultOf(job: Job): FetchResult | null {
  const result = job.result
  if (!isRecord(result)) return null
  const repositoryId = positiveInt(result.repository_id) ?? job.repository_id
  if (repositoryId === null || !Array.isArray(result.updated_refs)) return null
  const refs = result.updated_refs.map(refChangeOf).filter((r): r is RefChange => r !== null)
  const changed = result.changed_refs
  return {
    repository_id: repositoryId,
    updated_refs: refs,
    truncated: result.truncated === true,
    changed_refs: typeof changed === 'number' && Number.isInteger(changed) && changed >= 0 ? changed : undefined,
  }
}

/** 更新成功的結果句裡的「N 個 ref 變動」；N 優先用後端回報的實際變動數（可能比清單長）。 */
export function changedRefCount(result: FetchResult): number {
  return result.changed_refs ?? result.updated_refs.length
}

/** 成功的 GitHub 工作的結果句（「已註冊並複製完成」「已更新，3 個 ref 變動」）；不是這兩種工作或還沒成功時回傳 null。 */
export function githubSucceededText(job: Job): string | null {
  if (job.status !== 'succeeded') return null
  if (job.kind === 'github_register') {
    const outcome = registerOutcome(job)
    if (outcome.kind === 'created') return '已註冊並複製完成'
    if (outcome.kind === 'existing') return '這個 repo 已經註冊過，沒有重新複製'
    return '已完成'
  }
  if (job.kind === 'github_fetch') {
    const result = fetchResultOf(job)
    return result === null ? '已更新' : `已更新，${changedRefCount(result)} 個 ref 變動`
  }
  return null
}

/**
 * 從共用輪詢的資料取出「註冊」工作：進行中（新的在前），以及尚未忽略的失敗（最近失敗的在前；已被重試而回到進行中的不算）。
 * 註冊成功前沒有 repo，所以這些工作不會出現在任何 repo 卡片上（groupJobsByRepo 會略過），要在這裡另外列出。
 */
export function registrationJobs(
  active: readonly Job[],
  failed: readonly Job[],
  dismissed: ReadonlySet<number>,
): { active: Job[]; failed: Job[] } {
  const mine = active.filter((j) => j.kind === 'github_register' && (j.status === 'queued' || j.status === 'running'))
  const activeIds = new Set(active.map((j) => j.id))
  return {
    active: [...mine].sort((a, b) => b.id - a.id),
    failed: failed
      .filter((j) => j.kind === 'github_register' && j.status === 'failed' && !dismissed.has(j.id) && !activeIds.has(j.id))
      .sort((a, b) => failedAt(b) - failedAt(a) || b.id - a.id),
  }
}

/** 一個 repo 的工作分成「更新（github_fetch）」與「其他」：更新的提示顯示在卡片的操作區，其他的在卡片上方。 */
export function splitFetchJobs(jobs: RepoJobs): { fetch: RepoJobs; other: RepoJobs } {
  const isFetch = (j: Job) => j.kind === 'github_fetch'
  return {
    fetch: { active: jobs.active.filter(isFetch), failed: jobs.failed.filter(isFetch) },
    other: { active: jobs.active.filter((j) => !isFetch(j)), failed: jobs.failed.filter((j) => !isFetch(j)) },
  }
}
