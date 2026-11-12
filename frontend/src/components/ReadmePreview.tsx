import { useLayoutEffect, useRef, useState } from 'react'
import type { OverviewReadme } from '../types'

/**
 * README 預覽：純文字（white-space: pre-wrap），預設只顯示約 6 行，可展開全文。
 * README 內容是不可信資料：只用 React 的文字節點輸出（自動跳脫），不轉成 HTML 或 Markdown，
 * 也不使用 dangerouslySetInnerHTML。
 */
export function ReadmePreview({ readme }: { readme: OverviewReadme | null }) {
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const textRef = useRef<HTMLPreElement>(null)

  // 收合狀態下量一次：內容有沒有超出 6 行（沒超出就不需要展開鈕）
  useLayoutEffect(() => {
    const el = textRef.current
    if (el !== null && !expanded) setOverflowing(el.scrollHeight > el.clientHeight + 1)
  }, [readme?.text, expanded])

  if (readme === null) return <p className="muted">沒有 README。</p>
  if (readme.text.trim() === '') return <p className="muted">README 是空的（{readme.path}）。</p>

  return (
    <div className="readme">
      <div className="readme-path muted">{readme.path}</div>
      <div className="readme-box">
        <pre ref={textRef} className={expanded ? 'readme-text expanded' : 'readme-text'}>
          {readme.text}
        </pre>
      </div>
      {expanded && readme.truncated && <p className="note">README 過長，僅顯示前 4000 字元。</p>}
      {(overflowing || expanded) && (
        <button type="button" className="secondary small" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? '收合 README' : '展開 README 全文'}
        </button>
      )}
    </div>
  )
}
