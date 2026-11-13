// 對背景工作的動作（不含 React，只包 API 呼叫）：決定用背景工作還是同步回退、取消，以及取消後重新提交匯入。

import { api, ApiError, isAbortError } from './api'
import { toApiError } from './format'
import type { Job, JobSubmitResponse, JobsStatus } from './types'
import { chooseLoadMode, type LoadModeDecision } from './versionJobs'

/**
 * 送出背景工作前「現在」查一次 GET /jobs/status 並決定方式（不依賴輪詢的舊資料）：
 * worker 與 dispatcher 皆在線 → { mode: 'job' }；否則（含查不到狀態）→ { mode: 'sync', reason }。
 * action 是回退說明裡的動作（匯入、註冊、更新）。只有請求被中止（AbortError）才會拋出。
 */
export async function currentLoadMode(action: string, signal?: AbortSignal): Promise<LoadModeDecision> {
  try {
    const res = await api<JobsStatus>('GET', '/api/v1/jobs/status', { signal })
    return chooseLoadMode({ data: res.data, error: null, loading: false }, action)
  } catch (e) {
    if (isAbortError(e)) throw e
    return chooseLoadMode({ data: null, error: e, loading: false }, action)
  }
}

/**
 * POST /api/v1/jobs/{id}/cancel，回傳後端最新的工作：
 *   - 200：queued 已立即取消（status=cancelled），或早已取消；
 *   - 202：running，只記錄了取消請求（cancel_requested=true，status 仍是 running），要等 worker 在批次之間停下來；
 *   - 409 job_not_cancellable：工作已結束（成功、失敗），不算錯誤——重新讀取並回傳最終結果；
 *   - 404 等其他錯誤：照樣拋出 ApiError（由呼叫端用 ErrorBox 顯示）。
 */
export async function cancelJob(jobId: number): Promise<Job> {
  try {
    return (await api<Job>('POST', `/api/v1/jobs/${jobId}/cancel`)).data
  } catch (e) {
    const error = toApiError(e)
    if (error.code !== 'job_not_cancellable') throw error
    return (await api<Job>('GET', `/api/v1/jobs/${jobId}`)).data
  }
}

/**
 * 取消後重新匯入同一個版本：POST /repositories/{id}/import-jobs。後端會把取消（或失敗）的同一個 job 重新排入
 * （同一個 job id，202）；若該版本的快照已存在則回 200（沒有 job）。回傳後端的回應給呼叫端判斷。
 */
export async function resubmitImport(repositoryId: number, sha: string): Promise<JobSubmitResponse> {
  const res = await api<JobSubmitResponse>('POST', `/api/v1/repositories/${repositoryId}/import-jobs`, { body: { revision: sha } })
  if (res.data.job === null && res.data.snapshot_id === null) {
    throw new ApiError(res.status, 'invalid_response', '後端沒有回傳背景工作。', res.requestId)
  }
  return res.data
}
