import { useLoad } from '../useLoad'
import { importText, kindLabel, lineRangeText, RELATION_TYPE_LABEL, RESOLUTION_LABEL, symbolFlags } from '../structure'
import type { SymbolDetail } from '../types'
import { ApiHint, ErrorBox } from '../ui'

/**
 * 符號詳情（唯讀）：精確位置、修飾詞、關係與同檔的 import。
 * 位置的欄位是 0-based 的 UTF-8 位元組位移；關係的名稱是原始文字，未解析者標「未解析」，不猜測。
 * 內容都是不可信資料，只當純文字顯示。
 */
export function SymbolDetailView({ snapshotId, symbolId }: { snapshotId: number; symbolId: number }) {
  const path = `/api/v1/snapshots/${snapshotId}/symbols/${symbolId}`
  const { data: s, loading, error } = useLoad<SymbolDetail>(path)
  const flags = s === null ? [] : symbolFlags(s)
  return (
    <div className="detail symbol-detail">
      <h4>符號詳情</h4>
      <ApiHint apis={[`GET ${path}`]} />
      {loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      {s && (
        <>
          <dl className="kv">
            <dt>名稱</dt>
            <dd>
              <code className="wrap">{s.qualified_name}</code>（{kindLabel(s.kind)}）
            </dd>
            <dt>檔案</dt>
            <dd>
              <code className="wrap">{s.path}</code>
            </dd>
            <dt>位置</dt>
            <dd>
              {lineRangeText(s.start_line, s.end_line)}；欄位 {s.start_column}–{s.end_column}（0-based，UTF-8 位元組位移）
              {s.leading_start_line !== null && <>；裝飾器從第 {s.leading_start_line} 行開始</>}
            </dd>
            {flags.length > 0 && (
              <>
                <dt>標記</dt>
                <dd>{flags.join('、')}</dd>
              </>
            )}
            {s.type_parameter_count > 0 && (
              <>
                <dt>型別參數</dt>
                <dd>{s.type_parameter_count} 個</dd>
              </>
            )}
          </dl>
          <h4>關係</h4>
          {s.relations.length === 0 ? (
            <p className="empty">沒有基底類別或裝飾器。</p>
          ) : (
            <table className="table compact">
              <tbody>
                {s.relations.map((r, i) => (
                  <tr key={i}>
                    <th scope="row">{RELATION_TYPE_LABEL[r.relation_type] ?? r.relation_type}</th>
                    <td>
                      <code>{r.target_name}</code>
                    </td>
                    <td className="muted">
                      {RESOLUTION_LABEL[r.resolution]}（第 {r.line} 行）
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <details>
            <summary>同檔的 import（{s.imports.length}）</summary>
            {s.imports.length === 0 ? (
              <p className="empty">這個檔案沒有 import。</p>
            ) : (
              <ul className="plain-list">
                {s.imports.map((ref, i) => (
                  <li key={i}>
                    <code>{importText(ref)}</code> <span className="muted">（第 {ref.line} 行）</span>
                  </li>
                ))}
              </ul>
            )}
            {s.imports.length >= 200 && <p className="note">只顯示前 200 筆。</p>}
          </details>
          <p className="note">
            {s.has_errors ? '這個符號位於有語法錯誤的檔案，內容可能不完整。' : '位置來自語法層級解析，不含語意分析。'}
          </p>
        </>
      )}
    </div>
  )
}
