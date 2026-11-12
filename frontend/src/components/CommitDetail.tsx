import { useEffect, useRef, useState } from 'react'
import { commitShortSha, formatCommitTime } from '../format'
import type { Commit } from '../types'

/**
 * 選取中的 commit：一列摘要（短 SHA、訊息、作者與日期），完整資訊（完整 SHA 可複製、父 commit）用「完整資訊」展開。
 * 放在版本區上方的操作列裡，所以要精簡；呼叫端用 key={sha}，換 commit 時展開狀態重置。
 */
export function CommitDetail({ sha, commit }: { sha: string; commit: Commit | null }) {
  const [copied, setCopied] = useState<'ok' | 'fail' | null>(null)
  const timer = useRef<number | undefined>(undefined)
  useEffect(() => () => window.clearTimeout(timer.current), [])

  async function copy() {
    try {
      await navigator.clipboard.writeText(sha)
      setCopied('ok')
    } catch {
      setCopied('fail') // 非安全環境或權限被拒：請使用者手動選取 SHA
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setCopied(null), 2500)
  }

  return (
    <div className="commit-detail">
      <p className="commit-line">
        <span className="muted">選取的 commit：</span>
        <code title={sha}>{commitShortSha(sha)}</code>
        {commit !== null ? (
          <>
            <span className="commit-line-subject" title={commit.subject}>
              {commit.subject === '' ? '（無訊息）' : commit.subject}
            </span>
            <span className="muted">
              {commit.author_name} · {formatCommitTime(commit.authored_at)}
            </span>
          </>
        ) : (
          <span className="muted">（這個 commit 尚未載入到右側的圖中，只顯示 SHA）</span>
        )}
      </p>
      <details className="commit-more">
        <summary>完整資訊</summary>
        <dl className="kv">
          <dt>SHA</dt>
          <dd>
            <code className="wrap">{sha}</code>{' '}
            <button type="button" className="secondary small" onClick={copy}>
              複製 SHA
            </button>{' '}
            <span role="status" className="muted">
              {copied === 'ok' && '已複製'}
              {copied === 'fail' && '無法自動複製，請手動選取上方的 SHA'}
            </span>
          </dd>
          {commit && (
            <>
              <dt>訊息</dt>
              <dd>{commit.subject === '' ? '（無訊息）' : commit.subject}</dd>
              <dt>作者</dt>
              <dd>{commit.author_name}</dd>
              <dt>日期</dt>
              <dd>{formatCommitTime(commit.authored_at)}</dd>
              <dt>父 commit</dt>
              <dd>
                {commit.parents.length === 0
                  ? '（無，最初的 commit）'
                  : commit.parents.map((p) => <code key={p}>{commitShortSha(p)}</code>)}
              </dd>
            </>
          )}
        </dl>
      </details>
    </div>
  )
}

/** 目前檢視的版本摘要：分支／標籤、短 SHA、訊息（版本區與檔案區都用）。 */
export function VersionSummary({ scopeLabel, sha, subject }: { scopeLabel: string; sha: string; subject: string | null }) {
  return (
    <p className="version-summary">
      <span className="muted">目前檢視的版本：</span>
      <span className="ref-badge ref-branch">{scopeLabel}</span>
      <code title={sha}>{commitShortSha(sha)}</code>
      {subject !== null && <span>{subject === '' ? '（無訊息）' : subject}</span>}
    </p>
  )
}
