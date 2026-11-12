import { useCallback, useRef, useState } from 'react'
import { isAbortError, type ApiError } from './api'
import { toApiError } from './format'

export interface ActionState {
  /** 動作進行中。 */
  busy: boolean
  error: ApiError | null
  /** 成功時的說明文字（work 回傳的字串）。 */
  notice: string | null
  /** 執行一個動作；已有動作進行中時直接忽略（重複點擊只會送出一次）。回傳是否成功執行完畢。 */
  run: (work: () => Promise<string | void>) => Promise<boolean>
  clear: () => void
}

/**
 * 「按一下按鈕送一個請求」的共用狀態：進行中、錯誤、成功訊息，並擋住重複點擊。
 * 用 ref 擋重複（state 要等下一次 render 才會更新，連按兩下時第二下還看不到 busy）。
 */
export function useAction(): ActionState {
  const running = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const run = useCallback(async (work: () => Promise<string | void>) => {
    if (running.current) return false
    running.current = true
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      const text = await work()
      if (typeof text === 'string') setNotice(text)
      return true
    } catch (e) {
      if (!isAbortError(e)) setError(toApiError(e))
      return false
    } finally {
      running.current = false
      setBusy(false)
    }
  }, [])

  const clear = useCallback(() => {
    setError(null)
    setNotice(null)
  }, [])

  return { busy, error, notice, run, clear }
}
