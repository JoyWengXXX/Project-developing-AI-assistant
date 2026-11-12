import { formatBytes, formatTime, SKIP_REASON_LABEL } from '../format'
import type { SnapshotDetail } from '../types'
import { ApiHint, ErrorBox, SnapshotStatusBadge } from '../ui'
import { useLoad } from '../useLoad'

export const STALE_HINT = '匯入設定已改變，這份快照不會被當成目前設定的結果。'

function CountTable(props: { title: string; counts: Record<string, number>; labels?: Record<string, string> }) {
  const entries = Object.entries(props.counts)
  return (
    <div>
      <h4>{props.title}</h4>
      {entries.length === 0 ? (
        <p className="empty">（空）</p>
      ) : (
        <table className="table compact">
          <tbody>
            {entries.map(([key, count]) => (
              <tr key={key}>
                <th scope="row">
                  <code>{key}</code>
                </th>
                <td>{count}</td>
                <td className="muted">{props.labels?.[key] ?? ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

/** 單一快照的完整資料（唯讀）：狀態、匯入設定、檔案與跳過統計。只在使用者展開時才掛載、才發請求。 */
export function SnapshotDetailView({ snapshotId, refreshKey }: { snapshotId: number; refreshKey: number }) {
  const { data: s, loading, error } = useLoad<SnapshotDetail>(`/api/v1/snapshots/${snapshotId}`, refreshKey)
  return (
    <div className="detail">
      <h4>快照 #{snapshotId} 詳情</h4>
      <ApiHint apis={[`GET /api/v1/snapshots/${snapshotId}`]} />
      {loading && <p role="status">載入中…</p>}
      <ErrorBox error={error} />
      {s && (
        <>
          <dl className="kv">
            <dt>status</dt>
            <dd>
              <SnapshotStatusBadge status={s.status} />
            </dd>
            <dt>commit_sha</dt>
            <dd>
              <code className="wrap">{s.commit_sha}</code>
            </dd>
            <dt>import_profile_hash</dt>
            <dd>
              <code className="wrap">{s.import_profile_hash}</code>
              {s.is_stale_profile && <span className="muted">（過期設定：{STALE_HINT}）</span>}
            </dd>
            <dt>is_referenced</dt>
            <dd>{s.is_referenced ? '是（目前的參考版本）' : '否'}</dd>
            <dt>file_count</dt>
            <dd>{s.file_count}（已匯入）</dd>
            <dt>skipped_count</dt>
            <dd>{s.skipped_count}（已跳過）</dd>
            <dt>total_bytes</dt>
            <dd>{formatBytes(s.total_bytes)}</dd>
            <dt>started_at</dt>
            <dd>{formatTime(s.started_at)}</dd>
            <dt>completed_at</dt>
            <dd>{formatTime(s.completed_at)}</dd>
            <dt>error_summary</dt>
            <dd>{s.error_summary ?? '（無）'}</dd>
          </dl>
          {s.status !== 'completed' && (
            <p className="note">
              只有 completed 的快照才有檔案統計；此快照的統計為空，且檔案內容不可查詢（後端回 409）。
            </p>
          )}
          <div className="two-col">
            <CountTable title="file_status_counts" counts={s.file_status_counts} />
            <CountTable title="skip_reason_counts" counts={s.skip_reason_counts} labels={SKIP_REASON_LABEL} />
          </div>
        </>
      )}
    </div>
  )
}
