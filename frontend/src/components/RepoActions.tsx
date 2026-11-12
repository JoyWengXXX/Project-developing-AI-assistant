import { useEffect, useRef, useState } from 'react'
import { api, type ApiError } from '../api'
import { repoLabel, shortSha, toApiError } from '../format'
import type { FetchResult, RefChange, Repository } from '../types'
import { ErrorBox } from '../ui'

interface Props {
  repo: Repository
  /** 這一筆正在移除（DELETE 進行中，由總覽頁保存狀態）。 */
  removing: boolean
  /** 有任何一筆正在移除：避免同時送出多個移除。 */
  removeLocked: boolean
  /** 使用者在確認區塊按下「確定移除」；實際的 DELETE 與結果顯示由總覽頁負責。 */
  onRemove: (repo: Repository) => void
  /** fetch 成功後通知（卡片據此重新載入概況，App 據此重新整理版本區）。 */
  onFetched: (repoId: number) => void
}

const CHANGE_LABEL: Record<RefChange['change'], string> = {
  added: '新增',
  updated: '更新',
  removed: '移除',
}
const KIND_LABEL: Record<RefChange['kind'], string> = { branch: '分支', tag: 'tag' }

/** fetch 結果的 refs 摘要；truncated 時提示只顯示部分。 */
function FetchSummary({ result, repo }: { result: FetchResult; repo: Repository }) {
  const branch = repo.github_default_branch
  return (
    <div className="success" role="status">
      <p>
        {result.updated_refs.length === 0
          ? 'fetch 完成：沒有任何 ref 變動，已是最新。'
          : `fetch 完成：${result.updated_refs.length} 個 ref 有變動。`}
      </p>
      {result.updated_refs.length > 0 && (
        <ul className="ref-list">
          {result.updated_refs.map((r) => (
            <li key={`${r.kind}:${r.name}`}>
              <span className="badge badge-neutral">{CHANGE_LABEL[r.change]}</span> {KIND_LABEL[r.kind]}{' '}
              <code>{r.name}</code>{' '}
              <span className="muted">
                {r.old_sha ? shortSha(r.old_sha) : '（無）'} → {r.new_sha ? shortSha(r.new_sha) : '（無）'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {result.truncated && <p>refs 變動過多，只顯示部分（後端最多回報 100 筆）。</p>}
      <p>
        fetch 只更新遠端的分支與標籤，不會建立快照也不會自動匯入；這張卡片的概況已重新載入，若預設分支
        {branch && (
          <>
            （<code>{branch}</code>）
          </>
        )}
        有新的 commit，卡片會顯示「尚未匯入」或「有較新版本」，要不要匯入由你決定。
      </p>
    </div>
  )
}

/**
 * 卡片上的操作區：GitHub 來源專屬的「更新（fetch）」＋「移除」（需先確認）。
 * 這裡的按鈕都在卡片「可點擊主體」之外，點擊不會同時觸發「進入詳情」。
 */
export function RepoActions({ repo, removing, removeLocked, onRemove, onFetched }: Props) {
  const [fetching, setFetching] = useState(false)
  const [fetchResult, setFetchResult] = useState<FetchResult | null>(null)
  const [fetchError, setFetchError] = useState<ApiError | null>(null)
  const [confirming, setConfirming] = useState(false) // 是否顯示「確定移除？」區塊
  const removeButtonRef = useRef<HTMLButtonElement>(null)
  const cancelButtonRef = useRef<HTMLButtonElement>(null)
  const wasConfirming = useRef(false)

  // 鍵盤焦點：打開確認區塊時移到「取消」（預設走安全的選項）；取消後回到「移除」按鈕
  useEffect(() => {
    if (confirming) cancelButtonRef.current?.focus()
    else if (wasConfirming.current) removeButtonRef.current?.focus()
    wasConfirming.current = confirming
  }, [confirming])

  async function fetchRefs() {
    if (fetching) return
    setFetching(true)
    setFetchResult(null)
    setFetchError(null)
    try {
      const res = await api<FetchResult>('POST', `/api/v1/repositories/${repo.id}/fetch`)
      setFetchResult(res.data)
      onFetched(repo.id)
    } catch (err) {
      setFetchError(toApiError(err))
    } finally {
      setFetching(false)
    }
  }

  const isGitHub = repo.source_type === 'github'

  return (
    <div className="repo-actions">
      <div className="action-row">
        {isGitHub && (
          <button type="button" className="secondary" disabled={fetching || removing} onClick={fetchRefs}>
            {fetching ? '更新中…' : '更新（fetch）'}
          </button>
        )}
        <button
          type="button"
          className="danger"
          ref={removeButtonRef}
          aria-expanded={confirming}
          disabled={fetching || removing || removeLocked}
          onClick={() => setConfirming(true)}
        >
          {removing && !confirming ? '移除中…' : '移除'}
        </button>
      </div>

      {confirming && (
        <div
          className="remove-confirm"
          role="group"
          aria-labelledby={`remove-title-${repo.id}`}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && !removing) setConfirming(false)
          }}
        >
          <p id={`remove-title-${repo.id}`}>
            <strong>確定要移除 {repoLabel(repo)} 嗎？</strong>
          </p>
          <p>
            {isGitHub
              ? '會刪除本機 clone 與所有快照，不影響 GitHub 上的 repo。'
              : '只會移除 RepoPilot 內的紀錄與快照，不會刪除你磁碟上的資料夾。'}
          </p>
          <div className="action-row">
            <button
              type="button"
              className="danger-solid"
              disabled={removing || removeLocked}
              onClick={() => onRemove(repo)}
            >
              {removing ? '移除中…' : '確定移除'}
            </button>
            <button
              type="button"
              className="secondary"
              ref={cancelButtonRef}
              disabled={removing}
              onClick={() => setConfirming(false)}
            >
              取消
            </button>
          </div>
          {removing && (
            <p role="status" aria-busy="true">
              {isGitHub ? '正在刪除本機 clone，可能需要數秒；' : ''}這是同步請求，請等待後端回應。
            </p>
          )}
        </div>
      )}

      {isGitHub && fetching && (
        <p role="status" aria-busy="true">
          fetch 中…這是同步請求，請等待後端回應。
        </p>
      )}
      {fetchResult && <FetchSummary result={fetchResult} repo={repo} />}
      <ErrorBox error={fetchError} />
    </div>
  )
}
