import { useState } from 'react'
import { withQuery } from '../api'
import { errorPositionText, LANGUAGE_LABEL, PARSE_STATUS_LABEL } from '../structure'
import type { IndexedFileList } from '../types'
import { ApiHint, ErrorBox, Pager } from '../ui'
import { useLoad } from '../useLoad'

const PAGE = 20

/**
 * 「部分解析」與無法解析的檔案清單：路徑、狀態、第一個語法錯誤的位置。
 * 只有位置，沒有程式碼片段或錯誤訊息（後端刻意不保存，避免把可能含秘密的內容放進索引）。
 */
export function ProblemFiles({ snapshotId }: { snapshotId: number }) {
  const [offset, setOffset] = useState(0)
  const base = `/api/v1/snapshots/${snapshotId}/index/files`
  const { data, loading, error } = useLoad<IndexedFileList>(withQuery(base, { has_problems: 'true', limit: PAGE, offset }))
  return (
    <div>
      <ApiHint apis={[`GET ${base}?has_problems=true`]} />
      {loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      {data && data.total === 0 && <p className="empty">所有被索引的檔案都已解析，沒有部分解析或失敗的檔案。</p>}
      {data && data.total > 0 && (
        <>
          <table className="table compact">
            <thead>
              <tr>
                <th scope="col">檔案</th>
                <th scope="col">語言</th>
                <th scope="col">狀態</th>
                <th scope="col">第一個錯誤</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.indexed_file_id}>
                  <td>
                    <code className="wrap">{f.path}</code>
                  </td>
                  <td>{LANGUAGE_LABEL[f.language] ?? f.language}</td>
                  <td>{PARSE_STATUS_LABEL[f.parse_status]}</td>
                  <td className="muted">{errorPositionText(f.first_error)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager total={data.total} limit={data.limit} offset={data.offset} onChange={setOffset} />
        </>
      )}
    </div>
  )
}
