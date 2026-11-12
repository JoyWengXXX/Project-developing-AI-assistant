import { useEffect, useRef, useState } from 'react'
import { api, ApiError, isAbortError } from './api'
import { toApiError } from './format'

/** 單次請求的逾時：超過就中止並視為失敗（之後照退避重試），避免畫面永遠停在「更新中」。 */
export const POLL_REQUEST_TIMEOUT_MS = 10_000
const BACKOFF_BASE_MS = 5_000
const BACKOFF_MAX_MS = 60_000

export interface PollState<T> {
  /** 這個 path 最近一次成功的資料；請求失敗時沿用舊資料（畫面不會突然清空）。 */
  data: T | null
  error: ApiError | null
  /** 這個 path 還沒有任何結果（首次載入，或剛換了 path）。 */
  loading: boolean
  /** 最近一次成功的時間（Date.now()）。 */
  updatedAt: number | null
  /** 失敗後，下一次重試排在幾毫秒之後（成功時為 null）。 */
  retryDelayMs: number | null
}

export interface PollOptions<T> {
  /** false＝不輪詢（例如面板收合）；改回 true 時立刻抓一次。 */
  enabled: boolean
  /** 改變這個值＝立刻重新抓取（手動「重新整理」、操作成功後）。 */
  reloadKey?: number | string
  /** 依最新資料決定「下一次」輪詢間隔（毫秒），例如有進行中工作時快、沒有時慢。 */
  intervalMs: (data: T | null) => number
}

interface Settled<T> {
  key: string
  data: T | null
  error: ApiError | null
  updatedAt: number | null
  retryDelayMs: number | null
}

/**
 * 對 GET 路徑做輪詢（規格 10.2）。行為：
 * - 一次只有一個請求在飛：回應回來、決定間隔後才排下一次（setTimeout 串接，不用 setInterval）。
 * - 頁面不可見（document.hidden）時中止進行中的請求並暫停；回到前景立刻補抓一次。
 * - 失敗不中止：以 5→10→20→40→60 秒退避（不短於正常間隔）；成功後恢復正常間隔。
 * - path／enabled／reloadKey 改變或元件卸載：中止請求、清除計時器。
 *   每次抓取都帶「世代」編號，過期的回應（已被取代或已中止）一律丟棄，不會覆蓋新狀態。
 * - path 改變時，舊 path 的資料不會被當成新 path 的結果顯示（loading 為 true）；
 *   reloadKey 改變則保留舊資料直到新資料回來，畫面不閃爍。
 */
export function usePolling<T>(path: string | null, options: PollOptions<T>): PollState<T> {
  const { enabled, reloadKey = 0, intervalMs } = options
  const key = path !== null && enabled ? path : null
  const [settled, setSettled] = useState<Settled<T> | null>(null)

  // 間隔函式每次 render 都可能是新的閉包；放進 ref，輪詢中永遠讀到最新的那個，又不必重啟輪詢
  const intervalRef = useRef(intervalMs)
  useEffect(() => {
    intervalRef.current = intervalMs
  })

  useEffect(() => {
    if (path === null || key === null) return

    let stopped = false
    let generation = 0
    let failures = 0
    let lastData: T | null = null
    let timer: ReturnType<typeof setTimeout> | undefined
    let controller: AbortController | null = null

    function pause() {
      generation += 1 // 讓進行中的那一次作廢
      clearTimeout(timer)
      timer = undefined
      controller?.abort()
      controller = null
    }

    async function tick() {
      if (stopped || document.hidden) return
      pause() // 保證同一時間只有一個請求與一個計時器
      const mine = generation
      const ctl = new AbortController()
      controller = ctl
      let timedOut = false
      const timeout = setTimeout(() => {
        timedOut = true
        ctl.abort()
      }, POLL_REQUEST_TIMEOUT_MS)

      let delay: number
      try {
        const res = await api<T>('GET', path as string, { signal: ctl.signal })
        if (stopped || mine !== generation) return
        failures = 0
        lastData = res.data
        setSettled({ key: key as string, data: res.data, error: null, updatedAt: Date.now(), retryDelayMs: null })
        delay = intervalRef.current(res.data)
      } catch (e) {
        if (stopped || mine !== generation) return
        if (isAbortError(e) && !timedOut) return
        failures += 1
        const error = timedOut
          ? new ApiError(null, 'request_timeout', `請求超過 ${POLL_REQUEST_TIMEOUT_MS / 1000} 秒沒有回應。`, null)
          : toApiError(e)
        delay = Math.max(intervalRef.current(lastData), Math.min(BACKOFF_MAX_MS, BACKOFF_BASE_MS * 2 ** (failures - 1)))
        setSettled((prev) => ({
          key: key as string,
          data: lastData,
          error,
          updatedAt: prev !== null && prev.key === key ? prev.updatedAt : null,
          retryDelayMs: delay,
        }))
      } finally {
        clearTimeout(timeout)
      }
      if (!stopped && mine === generation && !document.hidden) timer = setTimeout(() => void tick(), delay)
    }

    function onVisibilityChange() {
      if (document.hidden) pause()
      else void tick() // 回到前景：立刻補抓一次
    }

    document.addEventListener('visibilitychange', onVisibilityChange)
    void tick()
    return () => {
      stopped = true
      document.removeEventListener('visibilitychange', onVisibilityChange)
      pause()
    }
  }, [path, key, reloadKey])

  const current = settled !== null && settled.key === key ? settled : null
  return {
    data: current?.data ?? null,
    error: current?.error ?? null,
    loading: key !== null && current === null,
    updatedAt: current?.updatedAt ?? null,
    retryDelayMs: current?.retryDelayMs ?? null,
  }
}
