import type { GraphRow } from '../commitGraph'

// 版面常數：列高必須固定，SVG 才能與文字列對齊、相鄰列的線才能接上。
export const ROW_HEIGHT = 30
export const LANE_WIDTH = 18
/** 圖形欄最多畫幾條 lane；超過的部分會被裁切（極端的大型歷史才會發生）。 */
export const MAX_VISIBLE_LANES = 16
const PALETTE_SIZE = 8 // 對應 index.css 的 --graph-0 ～ --graph-7

const MID = ROW_HEIGHT / 2
const LEFT_PAD = 6 // 左側留白：選取外框與列左側的色條不要重疊
const laneX = (lane: number) => LEFT_PAD + lane * LANE_WIDTH + LANE_WIDTH / 2
const colorVar = (color: number) => `var(--graph-${color % PALETTE_SIZE})`

/** 從 (x1, y0) 畫到列底端 (x2, 列高) 的路徑片段（不含起點）：同一個 x 為直線；不同 x 為 S 形彎線。 */
function toBottom(x1: number, y0: number, x2: number): string {
  if (x1 === x2) return `V${ROW_HEIGHT}`
  const ctrlY = (y0 + ROW_HEIGHT) / 2
  return `C${x1} ${ctrlY} ${x2} ${ctrlY} ${x2} ${ROW_HEIGHT}`
}

function Line({ d, color, dashed }: { d: string; color: number; dashed: boolean }) {
  return (
    <path
      d={d}
      className="graph-line"
      style={{ stroke: colorVar(color) }}
      strokeDasharray={dashed ? '3 3' : undefined}
    />
  )
}

interface Props {
  row: GraphRow
  /** 整張圖的 lane 數（所有列同寬，文字才會對齊）。 */
  laneCount: number
  selected: boolean
}

/**
 * 一列的圖形欄（單一 SVG，由 layoutGraph 算好的線段直接畫出）。
 * 節點形狀不只靠顏色區分：一般 commit＝實心圓、合併 commit（多個父親）＝空心圓、
 * 最初的 commit（沒有父親）＝菱形；選取中的 commit 外加一圈外框。
 */
export function GraphCell({ row, laneCount, selected }: Props) {
  const width = LEFT_PAD + Math.min(laneCount, MAX_VISIBLE_LANES) * LANE_WIDTH
  const cx = laneX(row.lane)
  const color = colorVar(row.color)
  return (
    <svg
      className="graph-cell"
      width={width}
      height={ROW_HEIGHT}
      viewBox={`0 0 ${width} ${ROW_HEIGHT}`}
      aria-hidden="true"
      focusable="false"
    >
      {/* 直通的線；toLane 較小者在下半部彎回左邊併入主線（上半部仍是直線） */}
      {row.through.map((s) => (
        <Line
          key={`through-${s.fromLane}`}
          d={`M${laneX(s.fromLane)} 0V${MID}${toBottom(laneX(s.fromLane), MID, laneX(s.toLane))}`}
          color={s.color}
          dashed={s.dashed}
        />
      ))}
      {row.fromTop && <Line d={`M${cx} 0V${MID}`} color={row.fromTop.color} dashed={false} />}
      {row.toBottom.map((s, i) => (
        <Line
          key={`down-${i}`}
          d={`M${laneX(s.fromLane)} ${MID}${toBottom(laneX(s.fromLane), MID, laneX(s.toLane))}`}
          color={s.color}
          dashed={s.dashed}
        />
      ))}

      {selected && <circle cx={cx} cy={MID} r={8.5} className="graph-selected-ring" />}
      {row.parentCount === 0 ? (
        <path
          d={`M${cx} ${MID - 6}L${cx + 6} ${MID}L${cx} ${MID + 6}L${cx - 6} ${MID}Z`}
          className="graph-node"
          style={{ fill: color }}
        />
      ) : row.parentCount >= 2 ? (
        <circle cx={cx} cy={MID} r={5} className="graph-node graph-node-merge" style={{ stroke: color }} />
      ) : (
        <circle cx={cx} cy={MID} r={5} className="graph-node" style={{ fill: color }} />
      )}
    </svg>
  )
}
