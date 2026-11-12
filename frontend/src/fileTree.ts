// 檔案樹的純函式：把後端的「平面 path 清單」轉成樹，再攤平成「目前看得到的列」。
// 這裡不含 React，也不呼叫 API，輸入相同結果就相同，方便單獨驗證（類似 .NET 的純靜態方法）。

import type { SnapshotFileSummary } from './types'

export interface DirNode {
  kind: 'dir'
  /** 唯一識別，也是「展開狀態」的 key：`d:` + 完整目錄路徑（合併目錄鏈時取最深一層）。 */
  key: string
  /** 顯示名稱；合併目錄鏈時為 `a/b/c`。 */
  name: string
  children: TreeNode[]
  /** 這個目錄底下（含所有子孫）的檔案數。 */
  fileCount: number
}

export interface FileNode {
  kind: 'file'
  /** `f:` + 檔案 id（path 在快照內唯一，但用 id 最不會出錯）。 */
  key: string
  name: string
  file: SnapshotFileSummary
}

export type TreeNode = DirNode | FileNode

/** 攤平後的一列：level 從 1 開始（對應 aria-level）。 */
export interface VisibleRow {
  node: TreeNode
  level: number
  /** 父節點的 key；第一層為 null（鍵盤 ← 用它跳到父層）。 */
  parentKey: string | null
}

function compareNames(a: string, b: string): number {
  // numeric：file2 排在 file10 前面；zh-TW：讓中文排序結果固定
  return a.localeCompare(b, 'zh-TW', { numeric: true })
}

/** 資料夾在前、檔案在後；同類依名稱排序（名稱相同時用 key 保持結果固定）。 */
function compareNodes(a: TreeNode, b: TreeNode): number {
  if (a.kind !== b.kind) return a.kind === 'dir' ? -1 : 1
  return compareNames(a.name, b.name) || compareNames(a.key, b.key)
}

/** 合併目錄鏈（compact folders）：目錄只有一個子節點且那是目錄時，與它合併成 `a/b`。 */
function mergeChain(dir: DirNode): DirNode {
  let current = dir
  while (current.children.length === 1 && current.children[0].kind === 'dir') {
    const only = current.children[0]
    current = { ...only, name: `${current.name}/${only.name}` }
  }
  return current
}

/** 由下而上：合併目錄鏈、排序子節點、累計檔案數。 */
function finish(dir: DirNode, compact: boolean): void {
  if (compact) {
    dir.children = dir.children.map((c) => (c.kind === 'dir' ? mergeChain(c) : c))
  }
  let count = 0
  for (const child of dir.children) {
    if (child.kind === 'dir') {
      finish(child, compact)
      count += child.fileCount
    } else {
      count += 1
    }
  }
  dir.children.sort(compareNodes)
  dir.fileCount = count
}

/**
 * 以 `/` 分隔 path 建立目錄樹，回傳不顯示的根節點（root.children 才是第一層）。
 * 路徑片段原樣保留（含空白與中文），不做 trim 或正規化。
 */
export function buildTree(files: SnapshotFileSummary[], compact = true): DirNode {
  const root: DirNode = { kind: 'dir', key: 'd:', name: '', children: [], fileCount: 0 }
  const dirs = new Map<string, DirNode>() // 完整目錄路徑 -> 節點，避免每個檔案都從根往下找

  for (const file of files) {
    const segments = file.path.split('/')
    const fileName = segments[segments.length - 1]

    let parent = root
    let dirPath = ''
    for (let i = 0; i < segments.length - 1; i++) {
      dirPath = i === 0 ? segments[0] : `${dirPath}/${segments[i]}`
      let dir = dirs.get(dirPath)
      if (dir === undefined) {
        dir = { kind: 'dir', key: `d:${dirPath}`, name: segments[i], children: [], fileCount: 0 }
        dirs.set(dirPath, dir)
        parent.children.push(dir)
      }
      parent = dir
    }
    parent.children.push({ kind: 'file', key: `f:${file.id}`, name: fileName, file })
  }

  finish(root, compact)
  return root
}

/** 搜尋：path 含關鍵字（不分大小寫）的檔案；關鍵字為空時原樣回傳。 */
export function filterFiles(files: SnapshotFileSummary[], query: string): SnapshotFileSummary[] {
  const q = query.trim().toLowerCase()
  if (q === '') return files
  return files.filter((f) => f.path.toLowerCase().includes(q))
}

/** 所有目錄的 key（「全部展開」與搜尋時使用）。 */
export function collectDirKeys(root: DirNode): string[] {
  const keys: string[] = []
  const walk = (dir: DirNode) => {
    for (const child of dir.children) {
      if (child.kind === 'dir') {
        keys.push(child.key)
        walk(child)
      }
    }
  }
  walk(root)
  return keys
}

/** 第一層目錄的 key（預設展開）。 */
export function topLevelDirKeys(root: DirNode): string[] {
  return root.children.filter((c) => c.kind === 'dir').map((c) => c.key)
}

/** 依展開狀態攤平成目前看得到的列（深度優先、由上而下），鍵盤上下移動就是在這個陣列上移動。 */
export function flattenVisible(root: DirNode, expanded: ReadonlySet<string>): VisibleRow[] {
  const rows: VisibleRow[] = []
  const walk = (dir: DirNode, level: number, parentKey: string | null) => {
    for (const node of dir.children) {
      rows.push({ node, level, parentKey })
      if (node.kind === 'dir' && expanded.has(node.key)) walk(node, level + 1, node.key)
    }
  }
  walk(root, 1, null)
  return rows
}
