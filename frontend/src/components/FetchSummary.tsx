import { shortSha } from '../format'
import { changedRefCount } from '../githubJobs'
import type { FetchResult, RefChange, Repository } from '../types'

const CHANGE_LABEL: Record<RefChange['change'], string> = {
  added: '新增',
  updated: '更新',
  removed: '移除',
}
const KIND_LABEL: Record<RefChange['kind'], string> = { branch: '分支', tag: 'tag' }

/**
 * 更新（fetch）結果的 refs 變更清單；truncated 時提示只顯示部分。
 * 同步 fetch 的回應與背景工作 github_fetch 的 result 形狀相同，兩者共用這個元件。
 */
export function FetchSummary({ result, repo }: { result: FetchResult; repo: Repository }) {
  const branch = repo.github_default_branch
  const count = changedRefCount(result)
  return (
    <div className="success" role="status">
      <p>{count === 0 ? 'fetch 完成：沒有任何 ref 變動，已是最新。' : `fetch 完成：${count} 個 ref 有變動。`}</p>
      {result.updated_refs.length > 0 && (
        <ul className="ref-list">
          {result.updated_refs.map((r) => (
            <li key={`${r.kind}:${r.name}`}>
              <span className="badge badge-neutral">{CHANGE_LABEL[r.change]}</span> {KIND_LABEL[r.kind]} <code>{r.name}</code>{' '}
              <span className="muted">
                {r.old_sha ? shortSha(r.old_sha) : '（無）'} → {r.new_sha ? shortSha(r.new_sha) : '（無）'}
              </span>
            </li>
          ))}
        </ul>
      )}
      {result.truncated && <p>refs 變動過多，只顯示部分（後端最多回報 100 筆）。</p>}
      <p>
        fetch 只更新遠端的分支與標籤，不會移動本機 HEAD，不會建立快照也不會自動匯入；這張卡片的概況已重新載入，若預設分支
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
