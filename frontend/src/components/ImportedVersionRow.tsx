import { memo } from 'react'
import { commitShortSha, formatCommitTime } from '../format'
import type { RefBadge } from '../importedList'
import { formatSize } from '../jobFormat'
import type { SnapshotItem } from '../types'
import { STALE_HINT } from './SnapshotDetailView'

const MAX_REF_BADGES = 3 // 一列最多顯示幾個分支／標籤徽章，其餘折成「+N」

interface Props {
  snapshot: SnapshotItem
  checked: boolean
  /** 這個版本正是上方「選取的 commit」。 */
  current: boolean
  /** 指向這個 commit 的分支／標籤（由已載入的 refs 對得上才有）。 */
  refs: readonly RefBadge[]
  /** commit 訊息（已載入的 commits 中有才有；null＝不顯示）。 */
  subject: string | null
  disabled: boolean
  /** shiftKey：Shift＋點擊做範圍選取。 */
  onToggle: (snapshotId: number, shiftKey: boolean) => void
  onPick: (sha: string) => void
  onRemove: (snapshot: SnapshotItem) => void
}

/**
 * 清單的一列：核取方塊、（可點選的）commit 資訊、單列「移除」。
 * 第二行是匯入時間、檔案數與大小、狀態徽章（失敗／匯入中／過期設定／參考中；徽章一律有文字與符號，不只靠顏色）。
 * memo：切換一列的選取時，其他列不會重畫（父元件傳進來的函式必須是穩定的）。
 */
export const ImportedVersionRow = memo(function ImportedVersionRow({
  snapshot: s,
  checked,
  current,
  refs,
  subject,
  disabled,
  onToggle,
  onPick,
  onRemove,
}: Props) {
  const short = commitShortSha(s.commit_sha)
  const time = formatCommitTime(s.completed_at ?? s.started_at)
  return (
    <li className={current ? 'imported-row current' : 'imported-row'}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        aria-label={`選取版本 ${short}`}
        onChange={(e) => onToggle(s.id, e.nativeEvent instanceof MouseEvent && e.nativeEvent.shiftKey)}
      />
      <button
        type="button"
        className="imported-main"
        aria-current={current ? 'true' : undefined}
        title={`${s.commit_sha}\n點一下，在上方檢視並操作這個版本`}
        onClick={() => onPick(s.commit_sha)}
      >
        <span className="imported-line">
          <code>{short}</code>
          {refs.slice(0, MAX_REF_BADGES).map((r) => (
            <span
              key={`${r.kind}:${r.name}`}
              className={`ref-badge ref-${r.kind}${r.isDefault ? ' ref-default' : ''}`}
              title={`${r.kind === 'tag' ? '標籤' : '分支'} ${r.name}`}
            >
              {r.kind === 'tag' ? '🏷 ' : r.isDefault ? '★ ' : ''}
              {r.name}
            </span>
          ))}
          {refs.length > MAX_REF_BADGES && <span className="muted">+{refs.length - MAX_REF_BADGES}</span>}
          {subject !== null && <span className="imported-subject">{subject === '' ? '（無訊息）' : subject}</span>}
        </span>
        <span className="imported-meta">
          <span>{time}</span>
          {s.status === 'completed' && (
            <span title={`${s.total_bytes.toLocaleString('zh-TW')} B`}>
              {s.file_count.toLocaleString('zh-TW')} 個檔案 · {formatSize(s.total_bytes)}
            </span>
          )}
          {s.status === 'failed' && (
            <span className="badge badge-failed" title={s.error_summary ?? undefined}>
              <span aria-hidden="true">✖ </span>失敗
            </span>
          )}
          {s.status === 'importing' && <span className="badge badge-importing">匯入中</span>}
          {s.is_stale_profile && (
            <span className="badge badge-importing" title={STALE_HINT}>
              <span aria-hidden="true">⚠ </span>過期設定
            </span>
          )}
          {s.is_referenced && (
            <span className="badge badge-completed">
              <span aria-hidden="true">★ </span>參考中
            </span>
          )}
        </span>
      </button>
      <button type="button" className="danger small" disabled={disabled} aria-label={`移除版本 ${short}`} onClick={() => onRemove(s)}>
        移除
      </button>
    </li>
  )
})
