import { useDeferredValue, useMemo, useState, type ReactNode } from 'react'
import { buildTree, collectDirKeys, filterFiles, topLevelDirKeys } from '../fileTree'
import { FILE_STATUS_LABEL, formatBytes, SKIP_REASON_LABEL } from '../format'
import type { SnapshotFileDetail } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { MAX_FILES, useAllFiles } from '../useAllFiles'
import { useLoad } from '../useLoad'
import { FileTree } from './FileTree'

interface Props {
  snapshotId: number
  /** 目前檢視的版本（分支／commit／訊息）摘要，由版本區提供。 */
  summary: ReactNode
}

/** 單一檔案的唯讀檢視。content 為 null 時說明原因（status／skip_reason）。 */
function FileViewer({ snapshotId, fileId }: { snapshotId: number; fileId: number }) {
  const path = `/api/v1/snapshots/${snapshotId}/files/${fileId}`
  const { data: f, loading, error } = useLoad<SnapshotFileDetail>(path)
  return (
    <div className="viewer">
      <h3>檔案內容</h3>
      <ApiHint apis={[`GET ${path}`]} />
      {loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      {f && (
        <>
          <dl className="kv">
            <dt>path</dt>
            <dd>
              <code>{f.path}</code>
            </dd>
            <dt>status</dt>
            <dd>
              <code>{f.status}</code> {FILE_STATUS_LABEL[f.status] ?? ''}
            </dd>
            {f.skip_reason !== null && (
              <>
                <dt>skip_reason</dt>
                <dd>
                  <code>{f.skip_reason}</code> {SKIP_REASON_LABEL[f.skip_reason] ?? ''}
                </dd>
              </>
            )}
            <dt>byte_size</dt>
            <dd>{formatBytes(f.byte_size)}</dd>
            <dt>sha256</dt>
            <dd>{f.sha256 ? <code className="wrap">{f.sha256}</code> : '（無）'}</dd>
            <dt>git_blob_id</dt>
            <dd>{f.git_blob_id ? <code className="wrap">{f.git_blob_id}</code> : '（無）'}</dd>
          </dl>
          {f.content === null ? (
            <p className="note">
              此檔案沒有內容（content 為 null）：
              {f.skip_reason ? SKIP_REASON_LABEL[f.skip_reason] ?? f.skip_reason : '後端未提供原因'}。
            </p>
          ) : f.content === '' ? (
            <p className="note">（空檔案）</p>
          ) : (
            <pre className="content" tabIndex={0} aria-label={`${f.path} 的內容`}>
              {f.content}
            </pre>
          )}
        </>
      )}
    </div>
  )
}

/**
 * 檔案面板：以樹狀圖呈現所選版本（快照）的檔案，點檔案看內容。
 * - 樹需要完整清單，所以由 useAllFiles 逐頁抓完；status 篩選由後端做，搜尋由前端對已載入的 path 做。
 * - 不在前端判斷快照是否 completed——直接呼叫 API，非 completed 時顯示後端的 409。
 * - 快照一經完成就不會改變，所以不需要重新載入機制（換版本時版本區以 key 重建整個面板）。
 */
export function FilePanel({ snapshotId, summary }: Props) {
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const query = useDeferredValue(search) // 輸入框即時回應；大清單的過濾與建樹稍後再做
  const [fileId, setFileId] = useState<number | null>(null)
  // 展開狀態：null 代表「用預設」（一般＝只展開第一層；搜尋中＝全部展開）。
  // 一般瀏覽與搜尋各存一份，清除搜尋後會回到搜尋前的展開狀態。
  const [expanded, setExpanded] = useState<Set<string> | null>(null)
  const [searchExpanded, setSearchExpanded] = useState<Set<string> | null>(null)

  const all = useAllFiles(snapshotId, status, 0)
  const searching = query.trim() !== ''
  const shownFiles = useMemo(() => (all.files ? filterFiles(all.files, query) : null), [all.files, query])
  const root = useMemo(() => (shownFiles ? buildTree(shownFiles) : null), [shownFiles])

  const customExpanded = searching ? searchExpanded : expanded
  const setCustomExpanded = searching ? setSearchExpanded : setExpanded
  const expandedKeys = useMemo(() => {
    if (root === null) return new Set<string>()
    return customExpanded ?? new Set(searching ? collectDirKeys(root) : topLevelDirKeys(root))
  }, [root, customExpanded, searching])

  function toggleDir(key: string) {
    const next = new Set(expandedKeys)
    if (!next.delete(key)) next.add(key)
    setCustomExpanded(next)
  }

  return (
    <div className="advanced-body">
      {summary}
      <ApiHint
        apis={[
          `GET /api/v1/snapshots/${snapshotId}/files（逐頁載入，limit=200）`,
          `GET /api/v1/snapshots/${snapshotId}/files/{file_id}`,
        ]}
      />

      <div className="toolbar">
        <label>
          status 篩選（後端過濾，變更時重新載入）
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value)
              setExpanded(null)
              setSearchExpanded(null)
            }}
          >
            <option value="">全部</option>
            <option value="imported">imported（有內容）</option>
            <option value="skipped">skipped（無內容）</option>
          </select>
        </label>
        <label>
          搜尋檔名／路徑（不分大小寫）
          <input
            type="search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value)
              setSearchExpanded(null) // 每次搜尋都從「全部展開」開始
            }}
            placeholder="例：readme、src/api"
          />
        </label>
        <button type="button" disabled={root === null} onClick={() => root && setCustomExpanded(new Set(collectDirKeys(root)))}>
          全部展開
        </button>
        <button type="button" disabled={root === null} onClick={() => setCustomExpanded(new Set())}>
          全部收合
        </button>
      </div>

      {all.loading && (
        <div role="status">
          <p className="note">
            {all.total > 0 ? `載入檔案清單… 已載入 ${all.loaded.toLocaleString()} / 共 ${all.total.toLocaleString()}` : '載入檔案清單…'}
          </p>
          {all.total > 0 && <progress value={all.loaded} max={all.total} aria-label="檔案清單載入進度" />}
        </div>
      )}
      <ErrorBox error={all.error} />
      {all.truncated && (
        <p className="warning" role="alert">
          檔案數超過上限，只顯示前 {MAX_FILES.toLocaleString()} 筆（共 {all.total.toLocaleString()} 筆）；搜尋也只涵蓋這些檔案。
        </p>
      )}
      {all.files && all.files.length === 0 && (
        <p className="empty">{status ? '沒有符合此 status 的檔案。' : '這個版本沒有任何檔案。'}</p>
      )}
      {all.files && all.files.length > 0 && shownFiles && root && (
        <>
          <p className="note" role="status">
            {searching
              ? `符合「${query.trim()}」的檔案：${shownFiles.length.toLocaleString()} / ${all.files.length.toLocaleString()}`
              : `共 ${all.files.length.toLocaleString()} 個檔案`}
          </p>
          {shownFiles.length === 0 && <p className="empty">沒有路徑包含「{query.trim()}」的檔案，請換個關鍵字。</p>}
          <div className="files-layout">
            {shownFiles.length > 0 && (
              <div className="file-list">
                <FileTree
                  root={root}
                  expanded={expandedKeys}
                  selectedFileId={fileId}
                  onToggle={toggleDir}
                  onSelectFile={setFileId}
                />
              </div>
            )}
            {fileId !== null ? (
              <FileViewer key={fileId} snapshotId={snapshotId} fileId={fileId} />
            ) : (
              shownFiles.length > 0 && <p className="empty">點選左側樹中的檔案以查看內容。</p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
