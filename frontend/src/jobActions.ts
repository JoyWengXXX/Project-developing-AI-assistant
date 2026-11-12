// 對背景工作的兩個動作（不含 React，只包 API 呼叫）：取消，以及取消後重新提交匯入。

import { api, ApiError } from './api'
import { toApiError } from './format'
import type { Job, JobSubmitResponse } from './types'

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
