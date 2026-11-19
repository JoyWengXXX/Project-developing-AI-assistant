// 「匯入版本」用背景工作時的純邏輯（不含 React，方便單獨驗證）：
//   - 要用背景工作，還是回退到同步匯入；
//   - 同一個 job 的新舊回應取哪一份；
//   - 目前匯入方式的說明文字。
// 注意：這個檔案只匯入型別（不匯入其他模組的值），讓暫時的 Node 腳本可以直接載入它做驗證。

import type { Job, JobsStatus } from './types'

/** 這個版本是用哪種方式載入的（顯示在狀態列的小字）。 */
export type LoadMethod =
  | { kind: 'job' } // POST import-jobs，由 worker 背景匯入
  | { kind: 'reused' } // import-jobs 回 200：快照早就存在，沒有建立 job
  | { kind: 'sync'; reason: string } // 回退到（已棄用的）同步 POST /imports

export type LoadModeDecision =
  | { mode: 'wait' } // 還不知道 worker 是否在線（第一次的 jobs/status 還沒回來）：先不要送
  | { mode: 'job' }
  | { mode: 'sync'; reason: string }

/** 頁面共用 /jobs/status 輪詢（PollState）中，決定匯入方式需要的欄位。 */
export interface StatusSnapshot {
  data: JobsStatus | null
  error: unknown
  loading: boolean
}

/**
 * 決定這次載入要用背景工作還是同步回退：只有 worker 與 dispatcher「都」在線才用背景工作。
 * 取不到狀態（請求失敗）時也回退：沒有證據顯示 worker 可用，同步路徑一定能做事（只是會佔住請求）。
 * 失敗時 usePolling 會保留舊資料，但舊資料已不可信，所以有 error 就當成取不到。
 * action：回退說明裡的動作（「匯入」、GitHub 的「註冊」「更新」）：「…，改用同步註冊」。
 */
export function chooseLoadMode(status: StatusSnapshot, action = '匯入'): LoadModeDecision {
  const fallback = `改用同步${action}`
  const unknown = `無法取得背景工作執行器（worker）的狀態，${fallback}`
  if (status.error !== null) return { mode: 'sync', reason: unknown }
  if (status.data === null) return status.loading ? { mode: 'wait' } : { mode: 'sync', reason: unknown }
  const { worker, dispatcher } = status.data
  if (worker.status !== 'online') {
    const why = worker.status === 'stale' ? '心跳已逾時' : '未啟動'
    return { mode: 'sync', reason: `背景工作執行器（worker）${why}，${fallback}` }
  }
  if (dispatcher.status !== 'online') {
    const why = dispatcher.status === 'stale' ? '心跳已逾時' : '未啟動'
    return { mode: 'sync', reason: `派送器（dispatcher）${why}，${fallback}` }
  }
  return { mode: 'job' }
}

export function methodNote(method: LoadMethod): string {
  switch (method.kind) {
    case 'job':
      return '匯入方式：背景工作（由 worker 在背景匯入，進度約每 1.5 秒更新一次）。'
    case 'reused':
      return '匯入方式：此版本已有快照，直接使用（沒有建立背景工作）。'
    case 'sync':
      return `匯入方式：${method.reason}（同步匯入會一直占住請求，直到完成）。`
  }
}

export function isFinished(job: Job): boolean {
  return job.status === 'succeeded' || job.status === 'failed' || job.status === 'cancelled'
}

/**
 * 同一個 job 的兩份資料（POST 回應、輪詢回應）取較新的：用後端的 updated_at 比較，
 * 相同時以新的為準（例如重試後 updated_at 一定較大）。避免較晚抵達的舊回應把狀態倒退。
 */
export function newerJob(previous: Job | undefined, next: Job): Job {
  if (previous === undefined) return next
  const prev = Date.parse(previous.updated_at)
  const incoming = Date.parse(next.updated_at)
  if (Number.isNaN(prev) || Number.isNaN(incoming)) return next
  return incoming >= prev ? next : previous
}

/**
 * 在背景工作的狀態中，這個 job 是否還在進行（排隊或執行中）。
 * 暫時性失敗退避中的 job 後端會把它放回 queued，所以仍屬進行中。
 */
export function isJobActive(job: Job | undefined): boolean {
  return job !== undefined && !isFinished(job)
}
