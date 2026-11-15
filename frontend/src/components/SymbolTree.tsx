import { useState } from 'react'
import { withQuery } from '../api'
import { kindLabel, SYMBOL_PAGE_LIMIT, symbolFlags } from '../structure'
import type { SymbolList, SymbolSummary } from '../types'
import { ApiHint, ErrorBox, Pager } from '../ui'
import { useLoad } from '../useLoad'

interface TreeContext {
  snapshotId: number
  selectedId: number | null
  onSelect: (symbolId: number) => void
}

/** 某個符號的直接子符號（展開時才載入）；超過單頁上限只顯示前面的並說明。 */
function Children({ ctx, parentId }: { ctx: TreeContext; parentId: number }) {
  const path = withQuery(`/api/v1/snapshots/${ctx.snapshotId}/symbols`, { parent_id: parentId, limit: SYMBOL_PAGE_LIMIT })
  const { data, loading, error } = useLoad<SymbolList>(path)
  return (
    <>
      {loading && <p role="status" className="muted">載入中…</p>}
      <ErrorBox error={error} />
      {data && (
        <>
          <ul className="symbol-tree-list">
            {data.items.map((child) => (
              <Node key={child.symbol_id} ctx={ctx} symbol={child} />
            ))}
          </ul>
          {data.total > data.items.length && (
            <p className="note">只顯示前 {data.items.length} 個子符號（共 {data.total} 個）。</p>
          )}
        </>
      )}
    </>
  )
}

function Node({ ctx, symbol }: { ctx: TreeContext; symbol: SymbolSummary }) {
  const [open, setOpen] = useState(false)
  const flags = symbolFlags(symbol)
  const selected = ctx.selectedId === symbol.symbol_id
  return (
    <li className="symbol-node">
      <div className={`symbol-row${selected ? ' selected' : ''}`}>
        {symbol.has_children ? (
          <button
            type="button"
            className="secondary small symbol-toggle"
            aria-expanded={open}
            aria-label={`${open ? '收合' : '展開'} ${symbol.name}`}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? '−' : '+'}
          </button>
        ) : (
          <span className="symbol-toggle-spacer" aria-hidden="true" />
        )}
        <button
          type="button"
          className="link-button symbol-name"
          aria-current={selected ? 'true' : undefined}
          onClick={() => ctx.onSelect(symbol.symbol_id)}
        >
          <span className="muted">{kindLabel(symbol.kind)}</span> <code>{symbol.kind === 'module' ? symbol.path : symbol.name}</code>
        </button>
        <span className="muted symbol-meta">
          第 {symbol.start_line} 行{flags.length > 0 && `・${flags.join('、')}`}
          {symbol.parent_id === null && symbol.kind !== 'module' && `・${symbol.path}`}
        </span>
      </div>
      {open && symbol.has_children && <Children ctx={ctx} parentId={symbol.symbol_id} />}
    </li>
  )
}

/**
 * 符號樹：最上層是各檔的模組（依路徑排序、分頁），展開才逐層載入子符號。
 * pathPrefix：只看某個目錄底下的檔案（區分大小寫的前綴，不是萬用字元）。
 */
export function SymbolTree({
  snapshotId,
  pathPrefix,
  selectedId,
  onSelect,
}: {
  snapshotId: number
  pathPrefix: string
  selectedId: number | null
  onSelect: (symbolId: number) => void
}) {
  const [offset, setOffset] = useState(0)
  const base = `/api/v1/snapshots/${snapshotId}/symbols`
  const path = withQuery(base, { top_level: 'true', path_prefix: pathPrefix, limit: 50, offset })
  const { data, loading, error } = useLoad<SymbolList>(path)
  const ctx: TreeContext = { snapshotId, selectedId, onSelect }
  return (
    <div>
      <ApiHint apis={[`GET ${base}?top_level=true`, `GET ${base}?parent_id=…`]} />
      {loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      {data && data.total === 0 && (
        <p className="empty">{pathPrefix === '' ? '這個索引沒有符號。' : '這個路徑前綴底下沒有符號。'}</p>
      )}
      {data && data.total > 0 && (
        <>
          <ul className="symbol-tree-list symbol-tree-root" aria-label="符號樹">
            {data.items.map((s) => (
              <Node key={s.symbol_id} ctx={ctx} symbol={s} />
            ))}
          </ul>
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </>
      )}
    </div>
  )
}
