import { useState } from 'react'
import { lineRangeText } from '../qaQuestion'
import type { SnapshotPresence } from '../useSnapshotPresence'
import { QaSourceViewer } from './QaSourceViewer'

export interface SourceRef {
  repository: string
  snapshot_id: number
  commit_sha: string
  path: string
  start_line: number
  end_line: number
}

/**
 * 一個來源位置：repo、快照、完整 commit SHA（40 碼，不縮短）、路徑與起訖行。
 * 快照還在（presence＝exists）才是可點的連結，點了在下方定位到檔案內容；
 * 快照已被移除或查不到時，只用純文字顯示當時記錄的位置，不提供連結（點了也無處可去）。
 * 所有欄位都是不可信內容，只當文字節點顯示。
 */
export function QaSourceRef({ source, presence, code }: { source: SourceRef; presence: SnapshotPresence | undefined; code?: string }) {
  const [open, setOpen] = useState(false)
  const range = lineRangeText(source.start_line, source.end_line)
  const location = `${source.path}:${range}`
  return (
    <div className="qa-source">
      <div>
        {code !== undefined && <span className="badge badge-neutral qa-chunk-code">{code}</span>}
        <strong>{source.repository}</strong>・快照 #{source.snapshot_id}・<code className="wrap" title="完整 commit SHA">{source.commit_sha}</code>
      </div>
      <div>
        {presence === 'exists' ? (
          <button type="button" className="link-button" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
            <code className="wrap">{location}</code>
            <span className="muted">（{open ? '收合' : '點擊查看檔案內容'}）</span>
          </button>
        ) : (
          <>
            <code className="wrap">{location}</code>
            <span className="muted">
              {presence === 'missing'
                ? '（這個快照已被移除，只保留當時記錄的來源位置）'
                : presence === 'unknown'
                  ? '（無法確認快照是否還在，沒有提供連結）'
                  : '（確認快照中…）'}
            </span>
          </>
        )}
      </div>
      {open && presence === 'exists' && <QaSourceViewer source={source} />}
    </div>
  )
}
