// 「搜尋」面板的純邏輯（不含 React、不匯入執行期模組；只匯入型別，方便用 Node 腳本單獨驗證）。
// 只負責「怎麼組請求、怎麼顯示」：比對規則、分頁上限與逾時都在後端（規格 8），前端不重做；
// 這裡的檢查只是避免送出一定會被 422 擋下的請求。

import type { SearchPageBase } from './types'

export type SearchTab = 'files' | 'symbols' | 'text'

export const SEARCH_TAB_LABEL: Record<SearchTab, string> = {
  files: '檔名',
  symbols: '符號',
  text: '內容',
}

/** 每頁筆數（後端預設 20、最大 100；片段較佔版面，沿用預設）。 */
export const SEARCH_PAGE_LIMIT = 20
/** 後端 offset 的上限（超出回 422）。 */
export const SEARCH_MAX_OFFSET = 10_000
export const SEARCH_MAX_QUERY_CHARS = 200
/** path_prefix 的長度上限（後端 1024）。 */
export const SEARCH_MAX_PREFIX_CHARS = 1024

export const SEARCH_MATCH_NOTE = '區分大小寫、字面比對（不支援正規表示式或萬用字元）'
export const SEARCH_TIMEOUT_HINT = '縮小路徑範圍（path_prefix）或使用更長的關鍵字'

// 與 structure.ts 的 SYMBOL_KIND_LABEL 同一組（後端 kind 的全部值）
export const SEARCH_SYMBOL_KINDS = [
  'module',
  'namespace',
  'class',
  'struct',
  'record',
  'interface',
  'type_alias',
  'enum',
  'enum_member',
  'delegate',
  'function',
  'method',
  'constructor',
  'property',
  'field',
] as const

export const SEARCH_LANGUAGES = ['python', 'csharp', 'typescript', 'javascript', 'java'] as const

// ---------- q 的檢查 ----------

export type QueryProblem = 'empty' | 'blank' | 'line_break' | 'too_long'

export const QUERY_PROBLEM_TEXT: Record<QueryProblem, string> = {
  empty: '請輸入關鍵字。',
  blank: '關鍵字不能全是空白。',
  line_break: '關鍵字不能含換行（或 NUL）。',
  too_long: `關鍵字最多 ${SEARCH_MAX_QUERY_CHARS} 個字元。`,
}

/**
 * 後端對 q 的規則：1～200 字元、不 strip（前後空白有意義）、不可全空白、不可含換行或 NUL。
 * 字元數以碼點計（Python 的 len）。回傳 null 表示可以送出；後端仍是最終依據（空白的定義可能略有差異）。
 */
export function queryProblem(q: string): QueryProblem | null {
  if (q === '') return 'empty'
  if (q.includes('\n') || q.includes('\r') || q.includes('\0')) return 'line_break'
  if (q.trim() === '') return 'blank'
  if (Array.from(q).length > SEARCH_MAX_QUERY_CHARS) return 'too_long'
  return null
}

/** 輸入框是空的或還在輸入中（空白）時，不顯示錯誤訊息，只是不搜尋。 */
export function shouldWarnQuery(problem: QueryProblem | null): boolean {
  return problem === 'line_break' || problem === 'too_long'
}

// ---------- 請求路徑 ----------

export interface SearchFilters {
  /** files／text：路徑前綴（區分大小寫，不是萬用字元）；空字串＝不限。 */
  pathPrefix: string
  /** symbols：種類；空字串＝不限。 */
  kind: string
  /** symbols：語言；空字串＝不限。 */
  language: string
}

/**
 * 組出（不含 limit／offset 的）搜尋路徑；q 不合法時回傳 null（呼叫端就不送請求）。
 * 只帶該分頁用得到的篩選條件；q 原樣送出（不 trim）。
 */
export function buildSearchPath(snapshotId: number, tab: SearchTab, q: string, filters: SearchFilters): string | null {
  if (queryProblem(q) !== null) return null
  const params = new URLSearchParams()
  params.set('q', q)
  if (tab === 'symbols') {
    if (filters.kind !== '') params.set('kind', filters.kind)
    if (filters.language !== '') params.set('language', filters.language)
  } else if (filters.pathPrefix !== '') {
    params.set('path_prefix', filters.pathPrefix)
  }
  return `/api/v1/snapshots/${snapshotId}/search/${tab}?${params.toString()}`
}

/** 在 buildSearchPath 的結果後面加上 limit 與 offset。 */
export function pagePath(basePath: string, offset: number, limit: number = SEARCH_PAGE_LIMIT): string {
  return `${basePath}&limit=${limit}&offset=${offset}`
}

// ---------- 分頁 ----------

/** 下一頁的 offset：用回應的 offset 加上實際收到的筆數（不是請求的 limit）。 */
export function nextOffset(page: Pick<SearchPageBase<unknown>, 'offset' | 'items'>): number {
  return page.offset + page.items.length
}

export type MoreState = 'none' | 'available' | 'offset_limit'

/**
 * 能不能「載入更多」：後端說還有（has_more），而且下一頁的 offset 沒有超過上限 10,000；
 * 超過時要請使用者縮小查詢，不能再送請求（會是 422）。
 */
export function moreState(hasMore: boolean, next: number): MoreState {
  if (!hasMore) return 'none'
  return next > SEARCH_MAX_OFFSET ? 'offset_limit' : 'available'
}

// ---------- 命中標示 ----------

export interface Segment {
  text: string
  match: boolean
}

/**
 * 把片段切成「命中前／命中／命中後」。offset 與長度都是碼點（Unicode code point），
 * 不是 UTF-16 單位：用 Array.from 切，含 emoji 等補充平面字元時 String.slice 會位移錯誤。
 * matchLength 是 q 的碼點數。位移不合理時不標示（整段當一般文字），不丟例外。
 * 回傳值只是文字，由呼叫端以 React 文字節點與 <mark> 顯示（不可使用 innerHTML）。
 */
export function highlightSegments(snippet: string, matchOffset: number, matchLength: number): Segment[] {
  const chars = Array.from(snippet)
  const valid =
    Number.isInteger(matchOffset) && Number.isInteger(matchLength) && matchOffset >= 0 && matchLength > 0 && matchOffset < chars.length
  if (!valid) return snippet === '' ? [] : [{ text: snippet, match: false }]
  const end = Math.min(chars.length, matchOffset + matchLength)
  const parts: Segment[] = [
    { text: chars.slice(0, matchOffset).join(''), match: false },
    { text: chars.slice(matchOffset, end).join(''), match: true },
    { text: chars.slice(end).join(''), match: false },
  ]
  return parts.filter((p) => p.text !== '')
}

/** 片段前後要不要顯示「…」：前面被截掉＝視窗不是從行首開始；後面只能依 snippet_truncated 判斷。 */
export function ellipsisFlags(item: { snippet_truncated: boolean; snippet_offset: number }): { before: boolean; after: boolean } {
  return { before: item.snippet_truncated && item.snippet_offset > 0, after: item.snippet_truncated }
}
