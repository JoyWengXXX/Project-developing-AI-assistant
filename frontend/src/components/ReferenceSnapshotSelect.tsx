import { useState } from 'react'
import type { Repository } from '../types'
import { ErrorBox } from '../ui'
import { useSnapshotList } from '../useSnapshotList'

function SnapshotOptions({
  repo, value, onChange, snapshotKey,
}: {
  repo: Repository
  value: number | null
  onChange: (snapshotId: number | null, label: string | null) => void
  snapshotKey: number
}) {
  const snapshots = useSnapshotList(repo.id, 'completed', snapshotKey)
  return (
    <>
      <select
        aria-label="參考版本"
        value={value ?? ''}
        onChange={(e) => {
          const id = e.target.value === '' ? null : Number(e.target.value)
          const picked = (snapshots.items ?? []).find((s) => s.id === id)
          onChange(id, picked === undefined ? null : `${repo.name} @ ${picked.commit_sha.slice(0, 7)}`)
        }}
      >
        <option value="">（不使用參考版本）</option>
        {(snapshots.items ?? []).map((snapshot) => (
          <option key={snapshot.id} value={snapshot.id}>
            快照 #{snapshot.id}・{snapshot.commit_sha.slice(0, 12)}
          </option>
        ))}
      </select>
      <ErrorBox error={snapshots.error} />
    </>
  )
}

/** 選擇一個已匯入完成的參考版本（選填）。有參考版本時，系統會先分析它的語言與依賴作為推薦依據。 */
export function ReferenceSnapshotSelect({
  repos, snapshotId, onChange, snapshotKey,
}: {
  repos: Repository[]
  snapshotId: number | null
  onChange: (snapshotId: number | null, label: string | null) => void
  snapshotKey: number
}) {
  const [repoId, setRepoId] = useState<number | null>(null)
  const repo = repos.find((r) => r.id === repoId) ?? null
  return (
    <div className="form">
      <label>
        參考 repo（選填）
        <select
          value={repoId ?? ''}
          onChange={(e) => {
            setRepoId(e.target.value === '' ? null : Number(e.target.value))
            onChange(null, null)
          }}
        >
          <option value="">（不使用參考 repo）</option>
          {repos.map((r) => (
            <option key={r.id} value={r.id}>
              {r.name}
            </option>
          ))}
        </select>
      </label>
      {repo !== null && (
        <label>
          參考版本
          <SnapshotOptions repo={repo} value={snapshotId} onChange={onChange} snapshotKey={snapshotKey} />
        </label>
      )}
    </div>
  )
}
