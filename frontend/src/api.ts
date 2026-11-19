// 前端唯一的 fetch 入口：所有 API 呼叫都經過 api()，統一做兩件事：
//   1. 解析統一錯誤格式，失敗時拋出 ApiError（含 code、message、request_id、details）。
//   2. 取得回應標頭 X-Request-ID；用它可以到後端日誌（單行 JSON，含 request_id）找到同一次請求。
//
// 這裡不保存任何請求或回應內容（沒有呼叫紀錄、沒有快取、沒有 console 輸出）：
// 請求 body 可能含 GitHub token，只在 fetch 呼叫的瞬間存在。

import type { ErrorBody, ValidationDetail } from './types'

// ---------- 錯誤 ----------

/** 後端回傳非 2xx（或連線失敗）時拋出；欄位對應統一錯誤格式。 */
export class ApiError extends Error {
  status: number | null // 連線失敗時為 null
  code: string
  requestId: string | null
  details: ValidationDetail[] | undefined

  constructor(
    status: number | null,
    code: string,
    message: string,
    requestId: string | null,
    details?: ValidationDetail[],
  ) {
    super(message)
    this.status = status
    this.code = code
    this.requestId = requestId
    this.details = details
  }
}

/** 元件卸載或參數改變而取消請求，不是真正的錯誤，呼叫端應直接忽略。 */
export function isAbortError(e: unknown): boolean {
  return e instanceof DOMException && e.name === 'AbortError'
}

// ---------- 呼叫包裝 ----------

export interface ApiResult<T> {
  status: number // 2xx 的實際狀態碼，例如註冊的 201／200
  data: T // 204 No Content 時沒有內容，呼叫端請用 api<void>
  requestId: string | null
}

interface ApiOptions {
  body?: unknown
  signal?: AbortSignal
}

/** 組查詢字串；略過空字串與 undefined。 */
export function withQuery(path: string, params: Record<string, string | number | undefined>): string {
  const q = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') q.set(key, String(value))
  }
  const s = q.toString()
  return s ? `${path}?${s}` : path
}

function tryParseJson(text: string): unknown {
  try {
    return JSON.parse(text)
  } catch {
    return undefined
  }
}

function isErrorBody(value: unknown): value is ErrorBody {
  if (typeof value !== 'object' || value === null || !('error' in value)) return false
  const err = (value as { error: unknown }).error
  return (
    typeof err === 'object' &&
    err !== null &&
    typeof (err as { code?: unknown }).code === 'string' &&
    typeof (err as { message?: unknown }).message === 'string'
  )
}

export async function api<T>(
  method: 'GET' | 'POST' | 'PUT' | 'DELETE',
  path: string,
  options: ApiOptions = {},
): Promise<ApiResult<T>> {
  let response: Response
  let text: string
  try {
    response = await fetch(path, {
      method,
      headers: options.body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
    text = await response.text()
  } catch (e) {
    if (isAbortError(e)) throw e
    // 訊息刻意用固定文字，不帶入例外內容
    throw new ApiError(null, 'network_error', '無法連線到伺服器，請確認 Vite dev server 仍在執行。', null)
  }

  const requestId = response.headers.get('X-Request-ID')

  if (response.status === 204) {
    return { status: 204, data: undefined as T, requestId }
  }

  const parsed = tryParseJson(text)

  if (response.ok) {
    if (parsed === undefined) {
      throw new ApiError(response.status, 'invalid_json', '伺服器回應不是 JSON。', requestId)
    }
    return { status: response.status, data: parsed as T, requestId }
  }

  if (isErrorBody(parsed)) {
    const e = parsed.error
    throw new ApiError(
      response.status,
      e.code,
      e.message,
      e.request_id ?? requestId,
      // snapshot_index_not_ready 的 details 是物件（{ index_status }），介面不需要它：只保留 422 的陣列形式
      Array.isArray(e.details) ? e.details : undefined,
    )
  }
  // 不是統一錯誤格式：常見於後端沒啟動時，Vite proxy 自己回 502／500 且沒有統一格式的 body
  throw new ApiError(
    response.status,
    'non_standard_error',
    `伺服器回應 HTTP ${response.status}，但不是統一錯誤格式（後端可能沒有啟動）。`,
    requestId,
  )
}
