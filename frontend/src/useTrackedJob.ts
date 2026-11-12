import { useState } from 'react'
import type { Job } from './types'
import { usePolling } from './usePolling'
import { isFinished, newerJob } from './versionJobs'

/** 追蹤已取消請求的工作：輪詢的間隔（與詳情匯入區相同）。 */
const TRACK_POLL_MS = 1_500

/**
 * 從一份已知的 job 開始追蹤它，直到結束（成功、失敗、取消）為止：輪詢 GET /jobs/{id}，
 * 回傳最新的資料（以 updated_at 把關，較舊的回應不會讓狀態倒退）。已結束就不輪詢。
 * 用在「使用者按了取消，這個工作會離開共用輪詢的進行中清單」之後，仍要把最後的結果顯示給他看。
 */
export function useTrackedJob(initial: Job): Job {
  const [job, setJob] = useState(initial)
  const poll = usePolling<Job>(`/api/v1/jobs/${initial.id}`, { enabled: !isFinished(job), intervalMs: () => TRACK_POLL_MS })
  // 輪詢到的資料併入 state（render 期間調整；usePolling 已丟棄過期回應）
  const [seen, setSeen] = useState<Job | null>(null)
  const polled = poll.data
  if (polled !== null && polled !== seen) {
    setSeen(polled)
    setJob((prev) => newerJob(prev, polled))
  }
  return job
}
