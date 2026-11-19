/** 等待下一次輪詢；分頁隱藏時暫停，回到前景立即繼續。 */
export function waitForVisiblePoll(
  ms: number,
  signal: AbortSignal,
  registerWake: (wake: () => void) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('Aborted', 'AbortError'))
      return
    }

    let timer: ReturnType<typeof setTimeout> | undefined
    let done = false
    const cleanup = () => {
      clearTimeout(timer)
      document.removeEventListener('visibilitychange', onVisibilityChange)
      signal.removeEventListener('abort', onAbort)
    }
    const finish = () => {
      if (done || document.hidden) return
      done = true
      cleanup()
      resolve()
    }
    const onAbort = () => {
      if (done) return
      done = true
      cleanup()
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const onVisibilityChange = () => {
      if (document.hidden) clearTimeout(timer)
      else finish()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    signal.addEventListener('abort', onAbort, { once: true })
    registerWake(finish)
    if (!document.hidden) timer = setTimeout(finish, ms)
  })
}
