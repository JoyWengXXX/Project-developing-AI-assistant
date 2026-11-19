import { useState } from 'react'
import { repoLabel } from '../format'
import type { Repository, RetrievalIndex, SnapshotItem } from '../types'
import { ErrorBox } from '../ui'
import { useLoad } from '../useLoad'
import { useSnapshotList } from '../useSnapshotList'

export interface QaSelection {
  repo: Repository
  snapshot: SnapshotItem
}

/** 最多選 20 個版本（與後端 snapshot_ids 的上限一致；超過時後端會回 422）。 */
export const QA_MAX_SNAPSHOTS = 20
const VISIBLE_AT_FIRST = 20

const INDEX_LABEL: Record<RetrievalIndex['status'], string> = {
  none: '尚未建立檢索索引',
  building: '檢索索引建立中',
  failed: '檢索索引建立失敗',
  completed: '檢索索引已就緒',
}

function SnapshotRow({
  snapshot, checked, full, onToggle,
}: {
  snapshot: SnapshotItem
  checked: boolean
  full: boolean
  onToggle: () => void
}) {
  // 每個版本讀一次檢索索引狀態（唯讀）：只有索引就緒的版本才能選；是否真的可用仍由後端提問時決定
  const index = useLoad<RetrievalIndex>(`/api/v1/snapshots/${snapshot.id}/retrieval-index`)
  const ready = index.data?.status === 'completed'
  const disabled = !checked && (!ready || full)
  return (
    <li>
      <label className="check-label">
        <input type="checkbox" checked={checked} disabled={disabled} onChange={onToggle} />{' '}
        快照 #{snapshot.id}・<code>{snapshot.commit_sha.slice(0, 12)}</code>
        {snapshot.is_stale_profile && '（匯入設定已變更）'}
        {' '}
        <span className={ready ? 'badge badge-completed' : 'badge badge-neutral'}>
          {index.loading ? '檢查索引中…' : index.error !== null ? '無法確認索引' : index.data !== null ? INDEX_LABEL[index.data.status] : ''}
        </span>
        {index.data !== null && !ready && <span className="muted">（請先到「跨 repo 檢索與搜尋比較」建立）</span>}
      </label>
    </li>
  )
}

function RepoSnapshots({
  repo, selected, snapshotKey, onToggle,
}: {
  repo: Repository
  selected: QaSelection[]
  snapshotKey: number
  onToggle: (repo: Repository, snapshot: SnapshotItem) => void
}) {
  const snapshots = useSnapshotList(repo.id, 'completed', snapshotKey)
  const [showAll, setShowAll] = useState(false)
  const items = snapshots.items ?? []
  const shown = showAll ? items : items.slice(0, VISIBLE_AT_FIRST)
  return (
    <div>
      {snapshots.loading && snapshots.items === null && <p role="status">載入版本中…</p>}
      <ErrorBox error={snapshots.error} />
      {snapshots.truncated && <p className="warning">只載入最近 2,000 個版本。</p>}
      {snapshots.items?.length === 0 && <p className="empty">這個 repo 沒有已匯入完成的版本。</p>}
      <ul className="retrieval-snapshots">
        {shown.map((snapshot) => (
          <SnapshotRow
            key={snapshot.id}
            snapshot={snapshot}
            checked={selected.some((item) => item.snapshot.id === snapshot.id)}
            full={selected.length >= QA_MAX_SNAPSHOTS}
            onToggle={() => onToggle(repo, snapshot)}
          />
        ))}
      </ul>
      {!showAll && items.length > VISIBLE_AT_FIRST && (
        <button type="button" className="secondary small" onClick={() => setShowAll(true)}>
          顯示全部 {items.length} 個版本（目前只列出最新的 {VISIBLE_AT_FIRST} 個）
        </button>
      )}
    </div>
  )
}

/**
 * 選擇要提問的版本：可跨 repo、最多 20 個；只列出已匯入完成的版本，且只有檢索索引就緒的才能勾選。
 * 已選的版本保存在呼叫端（切換 repo 不會遺失）。
 */
export function QaSnapshotPicker({
  repos, selected, snapshotKey, onToggle,
}: {
  repos: Repository[]
  selected: QaSelection[]
  snapshotKey: number
  onToggle: (repo: Repository, snapshot: SnapshotItem) => void
}) {
  const [repoId, setRepoId] = useState('')
  const repo = repos.find((item) => String(item.id) === repoId)
  return (
    <div className="qa-picker">
      <label>
        選擇 repo
        <select value={repoId} onChange={(event) => setRepoId(event.target.value)}>
          <option value="">請選擇</option>
          {repos.map((item) => (
            <option key={item.id} value={item.id}>
              {repoLabel(item)}
            </option>
          ))}
        </select>
      </label>
      {repos.length === 0 && <p className="empty">尚未註冊任何 repo。</p>}
      {repo && <RepoSnapshots key={repo.id} repo={repo} selected={selected} snapshotKey={snapshotKey} onToggle={onToggle} />}
    </div>
  )
}
