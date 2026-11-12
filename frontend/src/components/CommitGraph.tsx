import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { layoutGraph } from '../commitGraph'
import type { Commit } from '../types'
import { CommitRow } from './CommitRow'

interface Props {
  commits: Commit[]
  selectedSha: string | null
  /** 本次瀏覽中新匯入的 commit（後端回傳的 imported 可能比它舊）。 */
  loadedShas: ReadonlySet<string>
  /** 正在匯入的 commit（顯示「匯入中」徽章）。 */
  loadingShas: ReadonlySet<string>
  defaultBranch: string | null
  onSelect: (sha: string) => void
  /** 從別處（已匯入的版本清單）選取了 commit：把它捲到圖的可見範圍內（只捲動圖本身，不捲動頁面）。seq 每次遞增，重複選同一個也會再捲一次。 */
  reveal?: { sha: string; seq: number } | null
}

const PAGE_JUMP = 10 // PageUp／PageDown 一次移動的列數

/**
 * commit 圖：listbox 語意，每個 commit 是一個 option。
 * 鍵盤：↑↓ 移動焦點、Home／End 跳到頭尾、PageUp／PageDown 跳 10 列；Enter 或空白鍵選取。
 * （選取只會顯示該 commit 的資訊，不會匯入；匯入要按「匯入此版本」。）
 */
export function CommitGraph({ commits, selectedSha, loadedShas, loadingShas, defaultBranch, onSelect, reveal = null }: Props) {
  const graph = useMemo(() => layoutGraph(commits), [commits])
  const listRef = useRef<HTMLDivElement>(null)
  const [focusSha, setFocusSha] = useState<string | null>(null)

  // 外部選取 commit 時，把那一列捲進圖的可見範圍（不在已載入的圖中就什麼都不做）
  useEffect(() => {
    const box = listRef.current
    if (box === null || reveal === null) return
    const row = box.querySelector<HTMLElement>(`[data-sha="${reveal.sha}"]`)
    if (row === null) return
    if (row.offsetTop < box.scrollTop || row.offsetTop + row.offsetHeight > box.scrollTop + box.clientHeight) {
      box.scrollTop = Math.max(0, row.offsetTop - box.clientHeight / 2)
    }
  }, [reveal])

  // Tab 進入圖時的落點：最後聚焦的列 → 選取中的列 → 第一列
  const has = (sha: string | null) => sha !== null && commits.some((c) => c.sha === sha)
  const tabStopSha = has(focusSha) ? focusSha : has(selectedSha) ? selectedSha : commits[0]?.sha

  const shaOf = (target: EventTarget) => (target as HTMLElement).closest<HTMLElement>('[role="option"]')?.dataset.sha

  function focusRow(sha: string) {
    listRef.current?.querySelector<HTMLElement>(`[data-sha="${sha}"]`)?.focus()
  }

  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>) {
    const sha = shaOf(e.target)
    const index = commits.findIndex((c) => c.sha === sha)
    if (index < 0) return
    let moveTo: number | null = null
    switch (e.key) {
      case 'ArrowDown':
        moveTo = Math.min(index + 1, commits.length - 1)
        break
      case 'ArrowUp':
        moveTo = Math.max(index - 1, 0)
        break
      case 'PageDown':
        moveTo = Math.min(index + PAGE_JUMP, commits.length - 1)
        break
      case 'PageUp':
        moveTo = Math.max(index - PAGE_JUMP, 0)
        break
      case 'Home':
        moveTo = 0
        break
      case 'End':
        moveTo = commits.length - 1
        break
      case 'Enter':
      case ' ':
        onSelect(commits[index].sha)
        break
      default:
        return // 其他按鍵（例如 Tab）交給瀏覽器
    }
    e.preventDefault()
    if (moveTo !== null) focusRow(commits[moveTo].sha)
  }

  // onSelect 必須是穩定的函式（父元件傳 setState）：CommitRow 是 memo，函式每次不同會讓每一列都重畫
  return (
    <>
      <div
        ref={listRef}
        className="commit-graph"
        role="listbox"
        aria-label="commit 圖（↑↓ 移動，Enter 或空白鍵選取）"
        onKeyDown={handleKeyDown}
        onFocus={(e) => {
          const sha = shaOf(e.target)
          if (sha) setFocusSha(sha)
        }}
      >
        {commits.map((commit, i) => (
          <CommitRow
            key={commit.sha}
            commit={commit}
            row={graph.rows[i]}
            laneCount={graph.laneCount}
            selected={commit.sha === selectedSha}
            tabStop={commit.sha === tabStopSha}
            imported={commit.imported || loadedShas.has(commit.sha)}
            loading={loadingShas.has(commit.sha)}
            defaultBranch={defaultBranch}
            onSelect={onSelect}
          />
        ))}
      </div>
      <p className="note graph-legend">
        ● 一般 commit　○ 合併 commit　◆ 最初的 commit
        {graph.hasUnloaded && '　┆ 虛線：更早的 commit 尚未載入（按「載入更多」）'}
      </p>
    </>
  )
}
