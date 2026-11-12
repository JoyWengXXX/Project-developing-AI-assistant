import { useEffect, useRef } from 'react'
import { repoTitle } from '../format'
import type { Repository } from '../types'

interface Props {
  repo: Repository
  onBack: () => void
}

/** 詳情頁頂端：回到總覽、目前的 repo。進入時把焦點移到這一區（下一個 Tab 就是「回到總覽」）。 */
export function RepoDetailHeader({ repo, onBack }: Props) {
  const headerRef = useRef<HTMLElement>(null)
  useEffect(() => {
    headerRef.current?.focus()
  }, [])

  const isGitHub = repo.source_type === 'github'
  return (
    <section id="detail-header" className="panel detail-header" aria-labelledby="detail-title" tabIndex={-1} ref={headerRef}>
      <button type="button" className="secondary" onClick={onBack}>
        ← 回到總覽
      </button>
      <h2 id="detail-title">{repoTitle(repo)}</h2>
      <span className="badges">
        <span className="badge badge-neutral">{isGitHub ? 'GitHub' : '本機'}</span>
        {isGitHub && repo.github_is_private === true && <span className="badge badge-importing">私有</span>}
        {isGitHub && repo.github_is_private === false && <span className="badge badge-completed">公開</span>}
      </span>
      <span className="muted">#{repo.id}</span>
    </section>
  )
}
