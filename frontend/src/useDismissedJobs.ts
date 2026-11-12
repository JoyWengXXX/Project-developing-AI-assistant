import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { addDismissed, DISMISSED_JOBS_KEY, parseDismissed, readDismissed, removeDismissed, writeDismissed } from './dismissedJobs'

export interface DismissedJobs {
  /** 已忽略的失敗工作編號。 */
  ids: ReadonlySet<number>
  dismiss: (jobId: number) => void
  /** 取消忽略（重試、重新排入，或使用者在完整列表按「取消忽略」）。 */
  restore: (jobId: number) => void
}

function browserStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null // 存取 localStorage 本身就可能拋例外（隱私設定）
  }
}

/**
 * 「已忽略的失敗工作」：存在 localStorage（`repopilot-dismissed-jobs`，只存編號陣列，最多 200 筆）。
 * 其他分頁改動時（storage 事件）會同步；dismiss／restore 的函式身分穩定，可放進 effect 的依賴列。
 */
export function useDismissedJobs(): DismissedJobs {
  const [list, setList] = useState<number[]>(() => readDismissed(browserStorage()))
  const latest = useRef(list) // 連續呼叫（同一個事件處理中）時也讀得到最新值

  const commit = useCallback((next: number[], persist: boolean) => {
    latest.current = next
    setList(next)
    if (persist) writeDismissed(browserStorage(), next)
  }, [])

  const dismiss = useCallback((jobId: number) => commit(addDismissed(latest.current, jobId), true), [commit])
  const restore = useCallback(
    (jobId: number) => {
      if (latest.current.includes(jobId)) commit(removeDismissed(latest.current, jobId), true)
    },
    [commit],
  )

  useEffect(() => {
    function onStorage(e: StorageEvent) {
      if (e.key === DISMISSED_JOBS_KEY || e.key === null) commit(parseDismissed(e.newValue), false)
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [commit])

  const ids = useMemo(() => new Set(list), [list])
  return { ids, dismiss, restore }
}
