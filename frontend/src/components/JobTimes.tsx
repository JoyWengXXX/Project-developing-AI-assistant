import { useState } from 'react'
import { formatTime } from '../format'
import { formatDuration, relativeTime, remainingMs } from '../jobFormat'
import { useNow } from '../useNow'

/** 相對時間（「3 分鐘前」，每 30 秒更新一次）；滑鼠停留時以 title 顯示絕對時間。label 說明這個時間是什麼（建立、結束）。 */
export function RelativeTime({ iso, label }: { iso: string; label: string }) {
  const now = useNow(null, 30_000)
  return (
    <time dateTime={iso} title={`${label}：${formatTime(iso)}`}>
      {relativeTime(iso, now)}
    </time>
  )
}

/**
 * 重試倒數（含說明文字）。只有「掛載當下 next_attempt_at 還在未來」才會顯示；
 * 倒數到點後改顯示「已到重試時間」（是否真的執行取決於後端的 worker）。
 * 呼叫端請用 key={next_attempt_at}，讓新的重試時間重新判斷。
 */
export function RetryCountdown({ targetIso }: { targetIso: string }) {
  const target = Date.parse(targetIso)
  const [wasInFuture] = useState(() => target > Date.now())
  const now = useNow(Number.isNaN(target) ? null : target)
  const left = remainingMs(targetIso, now)
  if (!wasInFuture || left === null) return null
  return (
    <p className="job-retry">
      <span aria-hidden="true">⏱ </span>
      重試倒數：{left > 0 ? <>約 {formatDuration(left)} 後重試</> : <>已到重試時間，等待下一次執行</>}
    </p>
  )
}

/** 從某個時間點到現在的已耗時（每秒更新）。 */
export function Elapsed({ sinceIso }: { sinceIso: string }) {
  const now = useNow()
  const since = Date.parse(sinceIso)
  return <>{Number.isNaN(since) ? '—' : formatDuration(now - since)}</>
}
