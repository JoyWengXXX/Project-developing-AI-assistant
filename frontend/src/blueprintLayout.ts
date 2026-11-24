// 專案藍圖的排版計算（純函式）：依賴分層、格線編號、修訂雲路徑。

export interface SheetModule {
  name: string
  purpose: string
  dependsOn: string[]
  /** 這個模組的檔案引用的參考位置（path:起-迄）。 */
  refs: string[]
}

export interface StackRow {
  label: string
  value: string | null
}

export const BOX_W = 164
export const BOX_H = 52
const GAP_X = 24
const SLOT = 220 // 每個模組欄位的寬度
export const ROW_H = 116
/** 寬螢幕每列 4 個模組；窄螢幕 2 個，讓圖紙縮放後字仍看得清楚。 */
export const WIDE_COLS = 4
export const NARROW_COLS = 2
/** 圖紙寬度（viewBox 單位）。 */
export function sheetWidth(cols: number): number {
  return cols * SLOT
}
/** 圖框上緣的欄號數量：是每列模組數的兩倍，讓置中排列的模組也各自落在不同欄。 */
export function zoneCols(cols: number): number {
  return cols * 2
}
export const TOP = 64
export const ZONE = 16 // 圖框外緣與內框之間的格線區（放列字母與欄號）
export const ROW_LETTERS = 'ABCDEFGHJKLMNPQRSTUVWXYZ'

/** 依中英文寬度截斷：CJK 算 1，ASCII 算 0.55。 */
export function fit(text: string, max: number): string {
  let width = 0
  for (let i = 0; i < text.length; i += 1) {
    width += text.charCodeAt(i) < 128 ? 0.55 : 1
    if (width > max) return `${text.slice(0, i)}…`
  }
  return text
}

/** 依賴深度分層：沒有依賴的在最底層，呼叫別人的往上排。循環依賴以已走訪的為準，不會無限遞迴。 */
function levels(modules: SheetModule[]): Map<string, number> {
  const names = new Set(modules.map((m) => m.name))
  const byName = new Map(modules.map((m) => [m.name, m]))
  const memo = new Map<string, number>()
  const visit = (name: string, trail: Set<string>): number => {
    const known = memo.get(name)
    if (known !== undefined) return known
    if (trail.has(name)) return 0
    trail.add(name)
    const deps = (byName.get(name)?.dependsOn ?? []).filter((d) => names.has(d) && d !== name)
    const level = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((d) => visit(d, trail)))
    trail.delete(name)
    memo.set(name, level)
    return level
  }
  modules.forEach((m) => visit(m.name, new Set()))
  return memo
}

export interface Placed extends SheetModule {
  x: number
  y: number
  zone: string
}

export function layout(modules: SheetModule[], cols: number): { placed: Placed[]; rows: number } {
  const W = sheetWidth(cols)
  const lv = levels(modules)
  const maxLevel = Math.max(0, ...lv.values())
  const rows: SheetModule[][] = []
  for (let level = maxLevel; level >= 0; level -= 1) {
    const inLevel = modules.filter((m) => lv.get(m.name) === level)
    for (let i = 0; i < inLevel.length; i += cols) rows.push(inLevel.slice(i, i + cols))
  }
  const placed: Placed[] = []
  rows.forEach((row, r) => {
    const rowWidth = row.length * BOX_W + (row.length - 1) * GAP_X
    const startX = (W - rowWidth) / 2
    row.forEach((m, c) => {
      const x = startX + c * (BOX_W + GAP_X)
      const col = Math.floor(((x + BOX_W / 2) / W) * zoneCols(cols)) + 1
      placed.push({ ...m, x, y: TOP + r * ROW_H, zone: `${ROW_LETTERS[r] ?? '?'}${col}` })
    })
  })
  return { placed, rows: rows.length }
}

/** 修訂雲：沿矩形順時針畫一串向外凸的圓弧。 */
export function cloudPath(x: number, y: number, w: number, h: number, step = 18): string {
  const points: [number, number][] = []
  const nx = Math.max(2, Math.round(w / step))
  const ny = Math.max(2, Math.round(h / step))
  for (let i = 0; i < nx; i += 1) points.push([x + (w * i) / nx, y])
  for (let i = 0; i < ny; i += 1) points.push([x + w, y + (h * i) / ny])
  for (let i = 0; i < nx; i += 1) points.push([x + w - (w * i) / nx, y + h])
  for (let i = 0; i < ny; i += 1) points.push([x, y + h - (h * i) / ny])
  const r = step * 0.62
  return `M${points[0][0]} ${points[0][1]} ${points
    .slice(1)
    .concat([points[0]])
    .map(([px, py]) => `A${r} ${r} 0 0 1 ${px} ${py}`)
    .join(' ')} Z`
}

export function zoneOf(modules: SheetModule[], name: string, cols: number): string | null {
  return layout(modules, cols).placed.find((m) => m.name === name)?.zone ?? null
}

