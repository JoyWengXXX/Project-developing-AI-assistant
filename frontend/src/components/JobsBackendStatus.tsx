import { queueWarning } from '../jobFormat'
import type { JobsStatus } from '../types'
import { ApiHint } from '../ui'
import type { PollState } from '../usePolling'
import { PollErrorBox } from './PollErrorBox'
import { WorkerStatusBadge } from './WorkerStatusBadge'

/**
 * 背景執行器是否在線：dispatcher／worker 的狀態、佇列數量，以及「有工作排隊但沒有 worker」的提示。
 * 離線時匯入會改用同步方式（請求會占住直到完成，畫面不會顯示進度）。
 */
export function JobsBackendStatus({ status }: { status: PollState<JobsStatus> }) {
  const data = status.data
  const warning = data === null ? null : queueWarning(data)
  return (
    <section className="jobs-backend" aria-labelledby="jobs-backend-title">
      <h3 id="jobs-backend-title">背景執行器是否在線</h3>
      <p className="note">
        在線時，匯入會交給背景執行器處理並顯示進度；離線時匯入會改用同步方式（請求會一直等到完成，畫面不會顯示進度）。
      </p>
      <ApiHint apis={['GET /api/v1/jobs/status（只讀資料庫的心跳紀錄，不連 Redis）']} />
      {status.loading && <p role="status">檢查中…</p>}
      <PollErrorBox state={status} />
      {data !== null && (
        <>
          <ul className="service-list">
            <li>
              <WorkerStatusBadge service="dispatcher" state={data.dispatcher} />
            </li>
            <li>
              <WorkerStatusBadge service="worker" state={data.worker} />
            </li>
          </ul>
          <p className="muted">
            排隊中 {data.queued} 個 · 執行中 {data.running} 個 · 心跳超過 {data.heartbeat_timeout_seconds} 秒沒有更新視為逾時
          </p>
          {warning !== null && (
            <div className="warning" role="status">
              <strong>
                <span aria-hidden="true">⚠ </span>
                {warning}
              </strong>
            </div>
          )}
          {data.worker.status === 'absent' && (
            <p className="note">背景執行器（worker）尚未啟動，匯入會改用同步方式。啟動方式見 README 的「第二階段：背景工作」。</p>
          )}
          {data.worker.status === 'stale' && (
            <p className="note">worker 曾經回報過心跳，但目前已逾時，可能已經停止。</p>
          )}
        </>
      )}
    </section>
  )
}
