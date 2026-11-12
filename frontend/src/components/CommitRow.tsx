import { memo } from 'react'
import type { GraphRow } from '../commitGraph'
import { commitShortSha, formatCommitTime } from '../format'
import type { Commit } from '../types'
import { GraphCell, ROW_HEIGHT } from './GraphCell'

interface Props {
  commit: Commit
  row: GraphRow
  laneCount: number
  selected: boolean
  /** roving tabindex：整張圖只有一列是 Tab 停靠點。 */
  tabStop: boolean
  /** 已匯入（後端說已有快照，或本次瀏覽中剛匯入）。 */
  imported: boolean
  /** 正在匯入（尚未完成；已匯入的優先顯示「已匯入」）。 */
  loading: boolean
  /** 預設分支的名稱（徽章強調用）。 */
  defaultBranch: string | null
  onSelect: (sha: string) => void
}

/**
 * commit 圖的一列：左側圖形欄（SVG）＋右側文字資訊。
 * 用 memo 包起來：選取或移動焦點時，只有受影響的兩列會重畫，500 列也不會卡。
 */
export const CommitRow = memo(function CommitRow({
  commit,
  row,
  laneCount,
  selected,
  tabStop,
  imported,
  loading,
  defaultBranch,
  onSelect,
}: Props) {
  const short = commitShortSha(commit.sha)
  const time = formatCommitTime(commit.authored_at)
  const subject = commit.subject === '' ? '（無訊息）' : commit.subject

  // 螢幕閱讀器念的完整描述（徽章與顏色的資訊都轉成文字）
  const label = [
    short,
    subject,
    `作者 ${commit.author_name}`,
    time,
    commit.refs.map((r) => `${r.kind === 'tag' ? '標籤' : '分支'} ${r.name}`).join('、'),
    row.parentCount >= 2 ? '合併 commit' : row.parentCount === 0 ? '最初的 commit' : '',
    imported ? '已匯入' : loading ? '匯入中' : '',
  ]
    .filter((s) => s !== '')
    .join('，')

  return (
    <div
      role="option"
      aria-selected={selected}
      aria-label={label}
      data-sha={commit.sha}
      tabIndex={tabStop ? 0 : -1}
      className={selected ? 'commit-row selected' : 'commit-row'}
      style={{ height: ROW_HEIGHT }}
      onClick={() => onSelect(commit.sha)}
    >
      <GraphCell row={row} laneCount={laneCount} selected={selected} />
      <div className="commit-text">
        {commit.refs.length > 0 && (
          <span className="ref-badges">
            {commit.refs.map((r) => {
              const isDefault = r.kind === 'branch' && r.name === defaultBranch
              const cls = `ref-badge ref-${r.kind}${isDefault ? ' ref-default' : ''}`
              return (
                <span key={`${r.kind}:${r.name}`} className={cls} title={`${r.kind === 'tag' ? '標籤' : '分支'} ${r.name}${isDefault ? '（預設分支）' : ''}`}>
                  {r.kind === 'tag' ? '🏷 ' : isDefault ? '★ ' : ''}
                  {r.name}
                </span>
              )
            })}
          </span>
        )}
        <code className="commit-sha" title={commit.sha}>
          {short}
        </code>
        <span className="commit-subject" title={commit.subject}>
          {subject}
        </span>
        <span className="commit-author" title={commit.author_name}>
          {commit.author_name}
        </span>
        <span className="commit-time">{time}</span>
        <span className="commit-loaded-slot">
          {imported ? (
            <span className="badge badge-completed">已匯入</span>
          ) : (
            loading && <span className="badge badge-importing">匯入中</span>
          )}
        </span>
      </div>
    </div>
  )
})
