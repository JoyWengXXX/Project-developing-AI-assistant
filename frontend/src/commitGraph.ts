// commit 圖的 lane（欄）配置：純函式，不依賴 React 或 DOM，方便單獨驗證。
//
// 輸入：依 `git log --topo-order` 排列的 commit（新的在上，子一定在父之前），每筆只需要 sha 與 parents。
// 輸出：每個 commit 一列（GraphRow），列出該列要畫的節點與線段；畫圖元件只負責把它們換成 SVG。
//
// 座標約定（每列固定高度，由畫圖元件決定像素）：
//   - 一列分上下兩半，節點在正中央；lane 0 在最左邊。
//   - fromTop：從「列的頂端」垂直連到節點（上一列延伸下來、等著這個 commit 的線）；分支的最新 commit 沒有。
//   - toBottom：從節點連到「列的底端」的某個 lane（通往父親的線；合併 commit 有多條，可能是彎線）。
//   - through：不經過節點、從頂端穿過到底端的線（別的分支的歷史）。通常是直線；
//     toLane 小於 fromLane 時，表示它在下半部彎向左邊、併入本列節點往下的那條主線（見下方演算法）。
// 相鄰兩列的接縫保證一致：第 i 列底端的 lane（含顏色、虛實）＝第 i+1 列頂端的 lane（驗證腳本有逐列檢查）。
//
// 演算法：由上往下掃，維護「每個 lane 正在等哪個 commit」。
//   - 一個 commit 出現時，等它的 lane 就是它的節點位置；沒人等它（分支最新端）就開一條新 lane。
//   - 它的每個父親：若已有 lane 在等該父親，就彎進那條 lane（本 lane 釋放，可被之後重用）；
//     否則第一個父親沿用本 lane，其餘父親開新 lane。
//   - 例外：第一個父親已被「右邊」的 lane 等著時，改成主線留在本 lane、右邊那條彎回來併入，讓主幹盡量靠左。
//   - 因此同一個 commit 最多只有一條 lane 在等它。

export interface GraphInput {
  sha: string
  parents: readonly string[]
}

export interface GraphSegment {
  fromLane: number
  toLane: number
  /** 顏色索引（0 起算，畫圖元件對應 CSS 變數 --graph-N；超過調色盤數量時循環）。 */
  color: number
  /** 這條線通往的父親不在目前載入的範圍內（分頁尚未載到），畫成虛線一路延伸到底。 */
  dashed: boolean
}

export interface GraphRow {
  sha: string
  lane: number
  color: number
  /** 此列的不重複父親數：0＝最初的 commit（root）、2 以上＝合併。 */
  parentCount: number
  fromTop: GraphSegment | null
  toBottom: GraphSegment[]
  through: GraphSegment[]
  /** 此列用到的 lane 數（最大 lane 索引 + 1）。 */
  laneCount: number
}

export interface CommitGraph {
  rows: GraphRow[]
  /** 整張圖用到的最大 lane 數（所有列對齊同一寬度）。 */
  laneCount: number
  /** 是否有線通往尚未載入的 commit（用來顯示「虛線＝尚未載入」的說明）。 */
  hasUnloaded: boolean
}

/** lane 上「正在等待某個 commit 出現」的線。 */
interface Lane {
  waitingFor: string // 這條線通往的 commit sha
  color: number
}

export function layoutGraph(commits: readonly GraphInput[]): CommitGraph {
  // sha → 列號：父親的列號在目前列之後才算「已載入」；不在範圍內（或順序異常）一律當作尚未載入
  const rowOf = new Map<string, number>()
  commits.forEach((c, i) => {
    if (!rowOf.has(c.sha)) rowOf.set(c.sha, i)
  })
  const isLoadedBelow = (sha: string, index: number) => (rowOf.get(sha) ?? -1) > index

  const lanes: (Lane | null)[] = [] // null＝空 lane，可重用
  let nextColor = 0 // 每開一條新 lane 就換下一個顏色索引（畫圖時再對調色盤取餘數）
  const rows: GraphRow[] = []
  let maxLanes = 1
  let hasUnloaded = false

  // 取最左邊的空 lane；沒有就往右加一條
  function freeLane(): number {
    const i = lanes.indexOf(null)
    if (i >= 0) return i
    lanes.push(null)
    return lanes.length - 1
  }

  commits.forEach((commit, index) => {
    // 1. 找到正在等這個 commit 的 lane；沒有就是分支的最新 commit，開新 lane
    let nodeLane = lanes.findIndex((l) => l !== null && l.waitingFor === commit.sha)
    let nodeColor: number
    let fromTop: GraphSegment | null = null
    if (nodeLane >= 0) {
      nodeColor = lanes[nodeLane]!.color
      fromTop = { fromLane: nodeLane, toLane: nodeLane, color: nodeColor, dashed: false }
    } else {
      nodeLane = freeLane()
      nodeColor = nextColor++
    }

    // 2. 其他有線的 lane 直通這一列（等的 commit 不在後面的列＝尚未載入，畫虛線）
    const through: GraphSegment[] = []
    let used = nodeLane + 1
    lanes.forEach((lane, i) => {
      if (lane === null || i === nodeLane) return
      used = Math.max(used, i + 1)
      through.push({ fromLane: i, toLane: i, color: lane.color, dashed: !isLoadedBelow(lane.waitingFor, index) })
    })
    lanes[nodeLane] = null // 節點所在 lane 先釋放，下面依父親重新指派

    // 3. 下半：每個（不重複的）父親一條線
    const parents = [...new Set(commit.parents)]
    const toBottom: GraphSegment[] = []
    parents.forEach((parent, j) => {
      const dashed = !isLoadedBelow(parent, index)
      if (dashed) hasUnloaded = true
      const existing = lanes.findIndex((l) => l !== null && l.waitingFor === parent)
      let target: number
      let color: number
      if (existing >= 0 && j === 0 && existing > nodeLane) {
        // 第一個父親已有位在「右邊」lane 的線在等它：主線留在左邊（本 lane），右邊那條改成在這一列彎回來併入，
        // 這樣主幹不會因為分支的 commit 排在前面就一路往右飄
        const shifted = through.find((s) => s.fromLane === existing)!
        shifted.toLane = nodeLane
        lanes[existing] = null
        target = nodeLane
        color = nodeColor
        lanes[target] = { waitingFor: parent, color }
      } else if (existing >= 0) {
        // 已有別的線在等這個父親：彎進那條 lane，本 lane 釋放
        target = existing
        color = lanes[existing]!.color
      } else if (j === 0) {
        // 第一個父親沿用本 lane 與顏色（主線）
        target = nodeLane
        color = nodeColor
        lanes[target] = { waitingFor: parent, color }
      } else {
        // 其他父親（被合併進來的分支）開新 lane、新顏色
        target = freeLane()
        color = nextColor++
        lanes[target] = { waitingFor: parent, color }
      }
      used = Math.max(used, target + 1)
      toBottom.push({ fromLane: nodeLane, toLane: target, color, dashed })
    })

    // 4. 收尾：移除右側的空 lane，讓之後的新 lane 盡量靠左
    while (lanes.length > 0 && lanes[lanes.length - 1] === null) lanes.pop()

    maxLanes = Math.max(maxLanes, used)
    rows.push({
      sha: commit.sha,
      lane: nodeLane,
      color: nodeColor,
      parentCount: parents.length,
      fromTop,
      toBottom,
      through,
      laneCount: used,
    })
  })

  return { rows, laneCount: maxLanes, hasUnloaded }
}
