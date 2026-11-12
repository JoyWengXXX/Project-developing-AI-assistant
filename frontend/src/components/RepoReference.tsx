import { useState } from 'react'
import { commitShortSha, formatTime } from '../format'
import { formatSize } from '../jobFormat'
import type { ReferenceItem, RepositoryOverview } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { clearReference, setReference } from '../useReferences'
import type { SnapshotListState } from '../useSnapshotList'

interface Props {
  repoId: number
  title: string
  overview: RepositoryOverview | null
  /** 預設分支名稱（概況或註冊記錄對得上時才有值）。 */
  defaultBranch: string | null
  reference: ReferenceItem | null
  /** 這個 repo 已匯入完成的版本（GET /repositories/{id}/snapshots?status=completed）。 */
  snapshots: SnapshotListState
  /** 「匯入並改用最新版本」進行中，或別的 repo 正在匯入：一次只做一個。 */
  importLocked: boolean
  /** 匯入預設分支最新版本，成功後自動設為參考版本。 */
  onSwitchToLatest: (headSha: string) => void
  /** 設定或取消參考成功後：重新讀取參考清單與已匯入版本。 */
  onChanged: () => void
}

/**
 * 卡片上的「參考版本」：目前的參考（分支名稱對得上才顯示，否則 commit 短 SHA）、
 * 「有較新版本」提示與捷徑，以及從已匯入版本選一個設為參考（或取消參考）。
 * 參考是固定的快照，不會自動跟著分支前進；它只是標記，尚未連到 Agent。
 */
export function RepoReference({
  repoId,
  title,
  overview,
  defaultBranch,
  reference,
  snapshots,
  importLocked,
  onSwitchToLatest,
  onChanged,
}: Props) {
  const action = useAction()
  const [pick, setPick] = useState('') // 下拉選到的快照 id（字串；'' 代表還沒選）

  const head = overview?.head ?? null
  const matchesHead = reference !== null && head !== null && reference.commit_sha === head.sha
  const hasNewer = reference !== null && head !== null && !matchesHead
  const versions = (snapshots.items ?? []).filter((s) => s.status === 'completed' && !s.is_stale_profile)
  const pickedId = pick === '' ? null : Number(pick)
  const canSet = pickedId !== null && pickedId !== reference?.snapshot_id && versions.some((s) => s.id === pickedId)

  // 成功或失敗都重新讀取：失敗常是資料已過期（例如版本剛被移除），重新讀取後選單才會是最新的
  async function set() {
    if (pickedId === null) return
    const ok = await action.run(async () => {
      await setReference(repoId, pickedId)
      return '已設為參考版本。'
    })
    if (ok) setPick('')
    onChanged()
  }

  async function clear() {
    await action.run(async () => {
      await clearReference(repoId)
      return '已取消參考版本。'
    })
    onChanged()
  }

  return (
    <section className="card-reference" aria-label={`${title} 的參考版本`}>
      <h4>參考版本</h4>
      {reference === null ? (
        <p className="muted">尚未設定參考版本。</p>
      ) : (
        <p>
          <code title={reference.commit_sha}>{commitShortSha(reference.commit_sha)}</code>{' '}
          {matchesHead && defaultBranch !== null && <span className="ref-badge ref-default">★ {defaultBranch}</span>}{' '}
          <span className="muted">
            {formatSize(reference.total_bytes)}，設定於 {formatTime(reference.created_at)}
          </span>
        </p>
      )}
      {hasNewer && head !== null && (
        <div className="warning" role="status">
          <p>
            <strong>有較新版本</strong>（預設分支最新 commit <code>{commitShortSha(head.sha)}</code>{' '}
            與參考版本不同）。參考版本是固定的，不會自動跟著前進。
          </p>
          <button type="button" disabled={importLocked || action.busy} onClick={() => onSwitchToLatest(head.sha)}>
            {overview?.analyzed ? '改用最新版本' : '匯入並改用最新版本'}
          </button>
        </div>
      )}

      {snapshots.items === null && snapshots.loading && <p className="note">載入已匯入的版本…</p>}
      <ErrorBox error={snapshots.error} />
      {snapshots.items !== null && versions.length === 0 && (
        <p className="note">這個 repo 還沒有已匯入的版本；匯入之後才能設為參考版本。</p>
      )}
      {versions.length > 0 && (
        <div className="reference-pick">
          <label>
            從已匯入的版本選擇
            <select value={pick} disabled={action.busy} onChange={(e) => setPick(e.target.value)}>
              <option value="">（請選擇）</option>
              {versions.map((s) => (
                <option key={s.id} value={s.id}>
                  {commitShortSha(s.commit_sha)} · {formatTime(s.completed_at)} · {formatSize(s.total_bytes)}
                  {head !== null && s.commit_sha === head.sha ? ' · 預設分支最新' : ''}
                  {s.id === reference?.snapshot_id ? ' · 參考中' : ''}
                </option>
              ))}
            </select>
          </label>
          <button type="button" disabled={!canSet || action.busy} onClick={() => void set()}>
            設為參考版本
          </button>
        </div>
      )}
      {reference !== null && (
        <button type="button" className="secondary" disabled={action.busy} onClick={() => void clear()}>
          取消參考
        </button>
      )}
      {action.notice !== null && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} />
    </section>
  )
}
