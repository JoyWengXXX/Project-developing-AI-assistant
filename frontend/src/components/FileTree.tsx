import { useRef, useState, type KeyboardEvent } from 'react'
import { flattenVisible, type DirNode, type TreeNode } from '../fileTree'
import { SKIP_REASON_LABEL, formatBytes } from '../format'

interface Props {
  /** 不顯示的根節點；root.children 是第一層。 */
  root: DirNode
  /** 展開中的目錄 key（`DirNode.key`）。 */
  expanded: ReadonlySet<string>
  selectedFileId: number | null
  onToggle: (dirKey: string) => void
  onSelectFile: (fileId: number) => void
}

// ---------- 圖示：內嵌 SVG，用 currentColor 跟著主題（淺色／暗黑）變色 ----------

function Chevron() {
  return (
    <svg className="tree-chevron" viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path d="M6 3.5 10.5 8 6 12.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

function FolderIcon({ open }: { open: boolean }) {
  return (
    <svg className="tree-icon tree-icon-folder" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      {open ? (
        <path d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 5.5V6H4.2a1 1 0 0 0-.95.68L1.5 12V4Zm2.2 3h11.4a.5.5 0 0 1 .48.64l-1.4 5A1 1 0 0 1 13.2 13.4H2.8l1.9-5.7Z" fill="currentColor" />
      ) : (
        <path d="M1.5 4A1.5 1.5 0 0 1 3 2.5h3l1.5 1.5H13A1.5 1.5 0 0 1 14.5 5.5v6A1.5 1.5 0 0 1 13 13H3a1.5 1.5 0 0 1-1.5-1.5V4Z" fill="currentColor" />
      )}
    </svg>
  )
}

function FileIcon() {
  return (
    <svg className="tree-icon tree-icon-file" viewBox="0 0 16 16" width="16" height="16" aria-hidden="true">
      <path d="M4 1.5h5L12.5 5v8.5a1 1 0 0 1-1 1h-7.5a1 1 0 0 1-1-1v-11a1 1 0 0 1 1-1Z" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
      <path d="M9 1.5V5h3.5" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
    </svg>
  )
}

// ---------- 徽章：後端 status 只有 imported／skipped，真正的分類在 skip_reason ----------

// 依嚴重程度分色：bad＝刻意排除／不安全，warn＝內容無法保存，info＝特殊 Git 項目
const SKIP_REASON_TONE: Record<string, 'bad' | 'warn' | 'info'> = {
  excluded: 'bad',
  unsafe_path: 'bad',
  binary: 'warn',
  invalid_utf8: 'warn',
  too_large: 'warn',
  symlink: 'info',
  submodule: 'info',
  unsupported: 'info',
}

function SkipBadge({ reason }: { reason: string }) {
  const tone = SKIP_REASON_TONE[reason] ?? 'info'
  return (
    <span className={`badge badge-skip-${tone}`} title={SKIP_REASON_LABEL[reason] ?? reason}>
      {reason}
    </span>
  )
}

// ---------- 樹 ----------

interface ItemsProps extends Omit<Props, 'root'> {
  nodes: TreeNode[]
  level: number
  tabStopKey: string | undefined
}

/** 遞迴輸出一層節點：目錄展開時，子節點放進 role="group"。 */
function TreeItems({ nodes, level, tabStopKey, expanded, selectedFileId, onToggle, onSelectFile }: ItemsProps) {
  return nodes.map((node) => {
    const rowStyle = { paddingLeft: 6 + (level - 1) * 16 }
    const tabIndex = node.key === tabStopKey ? 0 : -1 // roving tabindex：整棵樹只有一個 Tab 停靠點

    if (node.kind === 'dir') {
      const open = expanded.has(node.key)
      return (
        <li
          key={node.key}
          role="treeitem"
          data-key={node.key}
          tabIndex={tabIndex}
          aria-level={level}
          aria-expanded={open}
          aria-label={`${node.name} 資料夾，${node.fileCount} 個檔案`}
        >
          <div className="tree-row" style={rowStyle} onClick={() => onToggle(node.key)}>
            <span className={open ? 'tree-chevron-box open' : 'tree-chevron-box'}>
              <Chevron />
            </span>
            <FolderIcon open={open} />
            <span className="tree-label" title={node.name}>
              {node.name || '（空名稱）'}
            </span>
            <span className="tree-count" title={`${node.fileCount} 個檔案`}>
              {node.fileCount}
            </span>
          </div>
          {open && (
            <ul role="group">
              <TreeItems
                nodes={node.children}
                level={level + 1}
                tabStopKey={tabStopKey}
                expanded={expanded}
                selectedFileId={selectedFileId}
                onToggle={onToggle}
                onSelectFile={onSelectFile}
              />
            </ul>
          )}
        </li>
      )
    }

    const f = node.file
    const selected = f.id === selectedFileId
    return (
      <li
        key={node.key}
        role="treeitem"
        data-key={node.key}
        tabIndex={tabIndex}
        aria-level={level}
        aria-selected={selected}
        aria-label={f.skip_reason ? `${node.name}（${f.skip_reason}）` : node.name}
      >
        <div
          className={selected ? 'tree-row selected' : 'tree-row'}
          style={rowStyle}
          title={`${f.path}\nstatus：${f.status}${f.skip_reason ? ` / ${f.skip_reason}` : ''}\n大小：${formatBytes(f.byte_size)}`}
          onClick={() => onSelectFile(f.id)}
        >
          <span className="tree-chevron-box" />
          <FileIcon />
          <span className="tree-label">{node.name || '（空名稱）'}</span>
          {f.skip_reason && <SkipBadge reason={f.skip_reason} />}
        </div>
      </li>
    )
  })
}

/**
 * 檔案樹（類似 VS Code 檔案總管）。展開狀態與選取狀態由父元件保存，這裡只負責顯示與鍵盤操作：
 * ↑↓ 移動、→ 展開（已展開則進入第一個子項）、← 收合（已收合則回到父層）、
 * Enter／空白鍵：檔案＝選取、資料夾＝展開或收合；Home／End 跳到頭尾。
 */
export function FileTree({ root, expanded, selectedFileId, onToggle, onSelectFile }: Props) {
  const treeRef = useRef<HTMLUListElement>(null)
  const [focusKey, setFocusKey] = useState<string | null>(null)

  // 目前看得到的列（鍵盤移動的依據）
  const rows = flattenVisible(root, expanded)
  const selectedKey = selectedFileId === null ? null : `f:${selectedFileId}`
  const has = (key: string | null) => key !== null && rows.some((r) => r.node.key === key)
  // Tab 進入樹時的落點：最後聚焦的項目 → 選取中的檔案 → 第一列（項目被收合或過濾掉時自動退回）
  const tabStopKey = has(focusKey) ? focusKey! : has(selectedKey) ? selectedKey! : rows[0]?.node.key

  function focusItem(key: string) {
    treeRef.current?.querySelector<HTMLElement>(`[data-key="${CSS.escape(key)}"]`)?.focus()
  }

  function keyOf(target: EventTarget): string | undefined {
    return (target as HTMLElement).closest<HTMLElement>('[role="treeitem"]')?.dataset.key
  }

  function handleKeyDown(e: KeyboardEvent<HTMLUListElement>) {
    const key = keyOf(e.target)
    const index = rows.findIndex((r) => r.node.key === key)
    if (index < 0) return
    const { node, parentKey } = rows[index]
    const isOpen = node.kind === 'dir' && expanded.has(node.key)
    let moveTo: string | null | undefined

    switch (e.key) {
      case 'ArrowDown':
        moveTo = rows[index + 1]?.node.key
        break
      case 'ArrowUp':
        moveTo = rows[index - 1]?.node.key
        break
      case 'Home':
        moveTo = rows[0].node.key
        break
      case 'End':
        moveTo = rows[rows.length - 1].node.key
        break
      case 'ArrowRight':
        if (node.kind === 'dir') {
          if (!isOpen) onToggle(node.key)
          else moveTo = rows[index + 1]?.node.key // 已展開：進入第一個子項
        }
        break
      case 'ArrowLeft':
        if (isOpen) onToggle(node.key)
        else moveTo = parentKey
        break
      case 'Enter':
      case ' ':
        if (node.kind === 'dir') onToggle(node.key)
        else onSelectFile(node.file.id)
        break
      default:
        return // 其他按鍵（例如 Tab）交給瀏覽器處理
    }
    e.preventDefault()
    if (moveTo) focusItem(moveTo)
  }

  return (
    <ul
      ref={treeRef}
      className="tree"
      role="tree"
      aria-label="檔案樹"
      onKeyDown={handleKeyDown}
      onFocus={(e) => {
        const key = keyOf(e.target)
        if (key) setFocusKey(key) // 滑鼠點擊也會先聚焦，所以這裡同時涵蓋滑鼠與鍵盤
      }}
    >
      <TreeItems
        nodes={root.children}
        level={1}
        tabStopKey={tabStopKey}
        expanded={expanded}
        selectedFileId={selectedFileId}
        onToggle={onToggle}
        onSelectFile={onSelectFile}
      />
    </ul>
  )
}
