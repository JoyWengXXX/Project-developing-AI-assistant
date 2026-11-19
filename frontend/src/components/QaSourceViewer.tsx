import { useEffect, useMemo, useRef, useState } from 'react'
import { withQuery } from '../api'
import { formatBytes } from '../format'
import { lineRangeText, SOURCE_CONTEXT_LINES, sourceWindow } from '../qaQuestion'
import type { Page, SnapshotFileDetail, SnapshotFileSummary } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useLoad } from '../useLoad'

export interface SourceLocation {
  snapshot_id: number
  path: string
  start_line: number
  end_line: number
}

/** 檔案內容切成行（與後端以 "\n" 計行一致）；結尾的換行不算多一行。 */
function splitLines(content: string): string[] {
  const lines = content.split('\n')
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return lines
}

/**
 * 在快照裡定位引用的檔案內容：以路徑找到檔案（GET /snapshots/{id}/files?path_prefix=，取完全相同的路徑），
 * 再讀內容（GET /snapshots/{id}/files/{file_id}）。預設只顯示引用範圍與前後幾行，可展開整個檔案；引用的行用「▶」與底色標示。
 * 內容一律是文字節點（不當 HTML）。
 */
export function QaSourceViewer({ source }: { source: SourceLocation }) {
  const [full, setFull] = useState(false)
  const list = useLoad<Page<SnapshotFileSummary>>(
    withQuery(`/api/v1/snapshots/${source.snapshot_id}/files`, { path_prefix: source.path, limit: 5 }),
  )
  // 路徑依序排序，完全相同的路徑一定在前綴相同的檔案之前
  const file = list.data?.items.find((item) => item.path === source.path) ?? null
  const detail = useLoad<SnapshotFileDetail>(file === null ? null : `/api/v1/snapshots/${source.snapshot_id}/files/${file.id}`)
  const content = detail.data?.content ?? null
  const lines = useMemo(() => (content === null ? null : splitLines(content)), [content])
  const firstCited = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (full) firstCited.current?.scrollIntoView({ block: 'center' })
  }, [full])

  const total = lines?.length ?? 0
  const win = lines === null ? null : full ? { from: 1, to: total } : sourceWindow(total, source.start_line, source.end_line, SOURCE_CONTEXT_LINES)
  const outOfFile = lines !== null && source.start_line > total

  return (
    <div className="qa-source-viewer" role="region" aria-label={`${source.path} 的引用位置`}>
      <ApiHint apis={[`GET /api/v1/snapshots/${source.snapshot_id}/files?path_prefix=…`, 'GET /api/v1/snapshots/{id}/files/{file_id}']} />
      {(list.loading || (file !== null && detail.loading)) && <p role="status">載入檔案內容中…</p>}
      <ErrorBox error={list.error ?? detail.error} />
      {list.data !== null && file === null && <p className="warning">這個快照裡找不到路徑 <code className="wrap">{source.path}</code>。</p>}
      {detail.data !== null && content === null && (
        <p className="note">這個檔案沒有保存內容（{detail.data.skip_reason ?? '原因不明'}），無法顯示引用的行。</p>
      )}
      {lines !== null && win !== null && detail.data !== null && (
        <>
          <p className="note">
            {detail.data.path}・{formatBytes(detail.data.byte_size)}・共 {total.toLocaleString('zh-TW')} 行・引用第 {lineRangeText(source.start_line, source.end_line)} 行
            {outOfFile && '（超出目前檔案的行數）'}
          </p>
          <div className="qa-source-lines" tabIndex={0} aria-label={`${source.path} 第 ${win.from}–${win.to} 行`}>
            {win.from > 1 && <div className="qa-source-gap muted">⋮ 前面還有 {win.from - 1} 行</div>}
            {lines.slice(win.from - 1, win.to).map((text, index) => {
              const number = win.from + index
              const cited = number >= source.start_line && number <= source.end_line
              return (
                <div
                  key={number}
                  className={cited ? 'qa-source-line cited' : 'qa-source-line'}
                  ref={cited && number === source.start_line ? firstCited : undefined}
                >
                  <span className="qa-source-no" title={cited ? '引用的行' : undefined}>
                    {cited ? '▶' : ' '} {number}
                  </span>
                  <span className="qa-source-text">{text === '' ? ' ' : text}</span>
                </div>
              )
            })}
            {win.to < total && <div className="qa-source-gap muted">⋮ 後面還有 {total - win.to} 行</div>}
          </div>
          {total > win.to - win.from + 1 || full ? (
            <button type="button" className="secondary small" onClick={() => setFull((value) => !value)}>
              {full ? '只顯示引用附近' : '顯示整個檔案'}
            </button>
          ) : null}
        </>
      )}
    </div>
  )
}
