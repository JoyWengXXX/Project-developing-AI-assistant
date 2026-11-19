import { useId, useState } from 'react'
import type { ApiError } from '../api'
import { formatSize } from '../jobFormat'
import {
  buildSearchPath,
  ellipsisFlags,
  highlightSegments,
  moreState,
  QUERY_PROBLEM_TEXT,
  queryProblem,
  SEARCH_LANGUAGES,
  SEARCH_MATCH_NOTE,
  SEARCH_MAX_OFFSET,
  SEARCH_MAX_PREFIX_CHARS,
  SEARCH_SYMBOL_KINDS,
  SEARCH_TAB_LABEL,
  SEARCH_TIMEOUT_HINT,
  shouldWarnQuery,
  type SearchTab,
} from '../search'
import { kindLabel, LANGUAGE_LABEL, lineRangeText } from '../structure'
import type { FileMatch, SymbolMatch, TextMatch } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useDebounced, useSearch, type SearchState } from '../useSearch'

const DEBOUNCE_MS = 300
const TABS: readonly SearchTab[] = ['files', 'symbols', 'text']

/** 命中標示：片段以文字節點顯示，命中處用 <mark>（以碼點位移切開；repo 內容不可信，不使用 innerHTML）。 */
function Snippet({ hit, queryLength }: { hit: TextMatch; queryLength: number }) {
  const flags = ellipsisFlags(hit)
  return (
    <code className="search-snippet">
      {flags.before && '…'}
      {highlightSegments(hit.snippet, hit.match_offset, queryLength).map((seg, i) =>
        seg.match ? <mark key={i}>{seg.text}</mark> : <span key={i}>{seg.text}</span>,
      )}
      {flags.after && '…'}
    </code>
  )
}

function FileHit({ hit }: { hit: FileMatch }) {
  return (
    <li className="search-hit">
      <code className="wrap">{hit.path}</code>
      {hit.byte_size !== null && <span className="muted search-meta">{formatSize(hit.byte_size)}</span>}
    </li>
  )
}

function SymbolHit({ hit }: { hit: SymbolMatch }) {
  return (
    <li className="search-hit">
      <div>
        <span className="muted">{kindLabel(hit.kind)}</span> <code className="wrap">{hit.qualified_name}</code>
        {hit.has_errors && <span className="muted search-meta">可能不完整</span>}
      </div>
      <div className="muted search-meta">
        {LANGUAGE_LABEL[hit.language] ?? hit.language}・<code className="wrap">{hit.path}</code>・{lineRangeText(hit.start_line, hit.end_line)}
      </div>
    </li>
  )
}

function TextHit({ hit, queryLength }: { hit: TextMatch; queryLength: number }) {
  return (
    <li className="search-hit">
      <code className="wrap search-location">
        {hit.path}:{hit.line}
      </code>
      <div>
        <Snippet hit={hit} queryLength={queryLength} />
      </div>
    </li>
  )
}

/** 搜尋錯誤：索引未就緒與逾時各有專屬說明，其餘沿用統一的錯誤顯示。 */
function SearchError({ error }: { error: ApiError }) {
  if (error.code === 'snapshot_index_not_ready') {
    return (
      <p className="warning" role="alert">
        這個版本還沒有建立完成的程式結構索引（尚未建立、建立中或失敗），所以無法搜尋符號。請先到上方的「程式結構」區塊按「建立索引」，完成後再搜尋；檔名與內容搜尋不需要索引。
      </p>
    )
  }
  if (error.code === 'search_timeout') {
    return (
      <div className="warning" role="alert">
        <p>搜尋逾時，沒有結果（不是「沒有符合的內容」）。建議：{SEARCH_TIMEOUT_HINT}。</p>
        {error.requestId !== null && (
          <p className="muted">
            request_id：<code>{error.requestId}</code>
          </p>
        )}
      </div>
    )
  }
  return <ErrorBox error={error} />
}

function resultCountText(state: SearchState<unknown>): string {
  const loaded = state.items.length
  if (state.total !== null) return `已載入 ${loaded.toLocaleString('zh-TW')} 筆，共 ${state.total.toLocaleString('zh-TW')} 筆`
  return `已載入 ${loaded.toLocaleString('zh-TW')} 筆${state.hasMore ? '（還有更多，內容搜尋不提供總數）' : ''}`
}

/**
 * 「搜尋」：快照內的檔名、符號、內容搜尋（字面子字串、區分大小寫）。
 * 檔名與內容搜尋不需要結構索引；符號搜尋需要已完成的索引。輸入穩定 300 毫秒才送出；
 * 條件（關鍵字、篩選、分頁籤）一變就取消舊請求並從第一頁重來，舊回應不會顯示。
 * 以 key={snapshotId} 掛載：換快照時整個重置。
 */
export function SearchPanel({ snapshotId }: { snapshotId: number }) {
  const [tab, setTab] = useState<SearchTab>('files')
  const [qInput, setQInput] = useState('')
  const [prefixInput, setPrefixInput] = useState('')
  const [kind, setKind] = useState('')
  const [language, setLanguage] = useState('')
  const baseId = useId()

  const q = useDebounced(qInput, DEBOUNCE_MS)
  const pathPrefix = useDebounced(prefixInput.trim(), DEBOUNCE_MS)
  const basePath = buildSearchPath(snapshotId, tab, q, { pathPrefix, kind, language })
  const state = useSearch<FileMatch | SymbolMatch | TextMatch>(basePath)

  const inputProblem = queryProblem(qInput)
  const more = moreState(state.hasMore, state.next)
  const queryLength = Array.from(q).length
  const showPrefix = tab !== 'symbols'
  const tabId = (t: SearchTab) => `${baseId}-tab-${t}`
  const panelId = `${baseId}-panel`

  return (
    <div className="detail search-panel">
      <h4>搜尋（快照 #{snapshotId}）</h4>
      <ApiHint
        apis={[
          `GET /api/v1/snapshots/${snapshotId}/search/files`,
          `GET /api/v1/snapshots/${snapshotId}/search/symbols`,
          `GET /api/v1/snapshots/${snapshotId}/search/text`,
        ]}
      />
      <p className="note">
        {SEARCH_MATCH_NOTE}。只搜尋這個版本已匯入的文字檔；被略過的檔案（二進位、過大等）不在範圍內。符號搜尋需要已建立的程式結構索引。
      </p>

      <div role="tablist" aria-label="搜尋類型" className="tabs">
        {TABS.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            id={tabId(t)}
            aria-selected={tab === t}
            aria-controls={panelId}
            className="tab"
            onClick={() => setTab(t)}
          >
            {SEARCH_TAB_LABEL[t]}
          </button>
        ))}
      </div>

      <div role="tabpanel" id={panelId} aria-labelledby={tabId(tab)}>
        <form className="inline-form" onSubmit={(e) => e.preventDefault()}>
          <label>
            關鍵字{' '}
            <input
              type="text"
              value={qInput}
              placeholder={tab === 'files' ? '路徑中的文字' : tab === 'symbols' ? '符號名稱' : '檔案內容中的文字'}
              aria-invalid={shouldWarnQuery(inputProblem)}
              onChange={(e) => setQInput(e.target.value)}
            />
          </label>
          {showPrefix && (
            <label>
              路徑前綴{' '}
              <input
                type="text"
                value={prefixInput}
                maxLength={SEARCH_MAX_PREFIX_CHARS}
                placeholder="例如 src/（區分大小寫）"
                onChange={(e) => setPrefixInput(e.target.value)}
              />
            </label>
          )}
          {!showPrefix && (
            <>
              <label>
                種類{' '}
                <select value={kind} onChange={(e) => setKind(e.target.value)}>
                  <option value="">全部</option>
                  {SEARCH_SYMBOL_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {kindLabel(k)}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                語言{' '}
                <select value={language} onChange={(e) => setLanguage(e.target.value)}>
                  <option value="">全部</option>
                  {SEARCH_LANGUAGES.map((l) => (
                    <option key={l} value={l}>
                      {LANGUAGE_LABEL[l]}
                    </option>
                  ))}
                </select>
              </label>
            </>
          )}
        </form>

        {inputProblem !== null && shouldWarnQuery(inputProblem) && (
          <p className="warning" role="alert">
            {QUERY_PROBLEM_TEXT[inputProblem]}
          </p>
        )}
        {basePath === null && inputProblem === 'blank' && <p className="note">關鍵字不能全是空白。</p>}
        {basePath === null && inputProblem === 'empty' && <p className="empty">輸入關鍵字開始搜尋。</p>}

        {state.loading && (
          <p role="status" aria-live="polite">
            搜尋中…
          </p>
        )}
        {state.error !== null && <SearchError error={state.error} />}

        {basePath !== null && !state.loading && state.error === null && (
          <>
            <p role="status" aria-live="polite" className="note">
              {state.items.length === 0 ? '沒有符合的結果。' : resultCountText(state)}
            </p>
            {state.items.length > 0 && (
              <ul className="search-results" aria-label={`${SEARCH_TAB_LABEL[tab]}搜尋結果`}>
                {state.items.map((item) =>
                  tab === 'files' ? (
                    <FileHit key={item.file_id} hit={item as FileMatch} />
                  ) : tab === 'symbols' ? (
                    <SymbolHit key={(item as SymbolMatch).symbol_id} hit={item as SymbolMatch} />
                  ) : (
                    <TextHit key={`${item.file_id}:${(item as TextMatch).line}`} hit={item as TextMatch} queryLength={queryLength} />
                  ),
                )}
              </ul>
            )}
            {state.moreError !== null && <SearchError error={state.moreError} />}
            {more === 'available' && (
              <div className="action-row">
                <button type="button" className="secondary" disabled={state.loadingMore} onClick={state.loadMore}>
                  {state.loadingMore ? '載入中…' : state.moreError !== null ? '重試載入更多' : '載入更多'}
                </button>
              </div>
            )}
            {more === 'offset_limit' && (
              <p className="warning" role="status">
                最多只能瀏覽前 {SEARCH_MAX_OFFSET.toLocaleString('zh-TW')} 筆，後面的結果無法繼續載入。請縮小查詢：
                {tab === 'symbols' ? '使用更長的關鍵字，或指定種類與語言。' : '使用更長的關鍵字，或加上路徑前綴。'}
              </p>
            )}
          </>
        )}
      </div>
    </div>
  )
}
