// GitHub 註冊與更新的送出（不含 React，只包 API 呼叫）。
// 決策與匯入相同（currentLoadMode）：worker 與 dispatcher 皆在線才送背景工作，否則回退到
// 已 deprecated 的同步端點（POST /repositories/github、POST /repositories/{id}/fetch）。

import { api, ApiError } from './api'
import { currentLoadMode } from './jobActions'
import type { FetchResult, Job, JobSubmitResponse, Repository } from './types'

export type RegisterSubmit =
  | { kind: 'job'; job: Job } // 202：背景工作已排入（或回既有的進行中工作）；repo 要等工作成功才存在
  | { kind: 'existing'; repositoryId: number } // 200：早已註冊，沒有建立工作，直接用這個 repo
  | { kind: 'sync'; repository: Repository; created: boolean; reason: string } // 回退的同步註冊（201 新建／200 已存在）

export type FetchSubmit =
  | { kind: 'job'; job: Job }
  | { kind: 'sync'; result: FetchResult; reason: string }

function missingJob(status: number, requestId: string | null): ApiError {
  return new ApiError(status, 'invalid_response', '後端沒有回傳背景工作。', requestId)
}

/**
 * 註冊 GitHub repo。onFallback：決定回退到同步註冊時，在送出（會一直等到 clone 完成）之前通知呼叫端，
 * 讓畫面在等待期間說明「這是同步請求」。
 */
export async function submitRegistration(
  input: string,
  options: { signal?: AbortSignal; onFallback?: (reason: string) => void } = {},
): Promise<RegisterSubmit> {
  const { signal, onFallback } = options
  const decision = await currentLoadMode('註冊', signal)
  if (decision.mode !== 'job') {
    const reason = decision.mode === 'sync' ? decision.reason : '改用同步註冊'
    onFallback?.(reason)
    const res = await api<Repository>('POST', '/api/v1/repositories/github', { body: { repo: input }, signal })
    return { kind: 'sync', repository: res.data, created: res.status === 201, reason }
  }

  const res = await api<JobSubmitResponse>('POST', '/api/v1/github/registration-jobs', { body: { repo: input }, signal })
  const { job, repository_id: repositoryId } = res.data
  // 200＝已註冊：沒有工作，直接用 repository_id（不輪詢、不 clone）
  if (res.data.reused || job === null) {
    if (repositoryId === null) throw missingJob(res.status, res.requestId)
    return { kind: 'existing', repositoryId }
  }
  return { kind: 'job', job }
}

/** 更新（fetch）一個 GitHub 來源的 repo。onFallback 同 submitRegistration。 */
export async function submitFetch(
  repoId: number,
  options: { signal?: AbortSignal; onFallback?: (reason: string) => void } = {},
): Promise<FetchSubmit> {
  const { signal, onFallback } = options
  const decision = await currentLoadMode('更新', signal)
  if (decision.mode !== 'job') {
    const reason = decision.mode === 'sync' ? decision.reason : '改用同步更新'
    onFallback?.(reason)
    const res = await api<FetchResult>('POST', `/api/v1/repositories/${repoId}/fetch`, { signal })
    return { kind: 'sync', result: res.data, reason }
  }

  const res = await api<JobSubmitResponse>('POST', `/api/v1/repositories/${repoId}/fetch-jobs`, { signal })
  if (res.data.job === null) throw missingJob(res.status, res.requestId)
  return { kind: 'job', job: res.data.job }
}
