import { useState, type KeyboardEvent } from 'react'
import {
  BOX_H, BOX_W, ROW_H, ROW_LETTERS, TOP, ZONE, cloudPath, fit, layout, sheetWidth, zoneCols, type Placed, type SheetModule,
  type StackRow,
} from '../blueprintLayout'

interface Props {
  title: string
  sheetNo: string
  status: string
  stack: StackRow[]
  modules: SheetModule[]
  /** 參考來源（repo @ commit），顯示在詳圖索引的說明框；沒有參考時為 null。 */
  refSource: string | null
  openQuestions: number
  selected: string | null
  onSelect: (name: string) => void
  /** 剛被回答而改變的標題欄列：短暫以琥珀色標示。 */
  changedLabel: string | null
  emptyText: string
  /** 每列模組數：寬螢幕 4、窄螢幕 2。 */
  cols: number
}

const BLOCK_W = 300
const NOTE_W = 320
const NOTE_LINE = 20
const NOTE_MAX = 4

function activate(e: KeyboardEvent<SVGGElement>, action: () => void) {
  if (e.key === 'Enter' || e.key === ' ') {
    e.preventDefault()
    e.stopPropagation()
    action()
  }
}

function noteLines(m: Placed, source: string | null) {
  const shown = m.refs.slice(0, NOTE_MAX)
  const more = m.refs.length - shown.length
  return [
    ...(source !== null ? [{ key: 'source', text: fit(source, 24), cls: 'bp-note-source' }] : []),
    ...shown.map((ref) => ({ key: ref, text: fit(ref, 22), cls: 'bp-note-ref' })),
    ...(more > 0 ? [{ key: 'more', text: `另有 ${more} 處，見下方表格`, cls: 'bp-note-source' }] : []),
  ]
}

function noteHeight(m: Placed, source: string | null): number {
  return 36 + noteLines(m, source).length * NOTE_LINE
}

/**
 * 詳圖索引的說明框：畫在圖面空白處（寬圖在標題欄左側、窄圖在標題欄下方），
 * 以琥珀色虛線引線連回索引圈，不遮住任何模組。
 */
function CalloutNote({ m, x, y, leader, source, onClose }: {
  m: Placed
  x: number
  y: number
  /** 引線路徑：由父元件沿圖框內緣繞行，不穿過任何文字。 */
  leader: string
  source: string | null
  onClose: () => void
}) {
  const lines = noteLines(m, source)
  const height = noteHeight(m, source)
  return (
    <g className="bp-note" role="dialog" aria-label={`${m.name} 的參考來源`}>
      <path d={leader} className="bp-note-leader" />
      <rect x={x} y={y} width={NOTE_W} height={height} />
      <text x={x + 12} y={y + 24} className="bp-note-title">
        {m.name} 的參考來源
      </text>
      <g
        className="bp-note-close"
        role="button"
        tabIndex={0}
        aria-label="關閉參考來源"
        onClick={(e) => {
          e.stopPropagation()
          onClose()
        }}
        onKeyDown={(e) => activate(e, onClose)}
      >
        <rect x={x + NOTE_W - 34} y={y + 6} width="28" height="24" />
        <path d={`M${x + NOTE_W - 25} ${y + 13} l10 10 M${x + NOTE_W - 15} ${y + 13} l-10 10`} />
      </g>
      {lines.map((line, i) => (
        <text key={`${i}-${line.key}`} x={x + 12} y={y + 24 + (i + 1) * NOTE_LINE} className={line.cls}>
          {line.text}
        </text>
      ))}
    </g>
  )
}

/**
 * 專案藍圖：一張有圖框、格線編號與標題欄的配置圖。模組依依賴分層排列（呼叫端在上），
 * 有參考來源的模組帶琥珀色詳圖索引圈（點圈在圖上顯示來源）；待確認事項以修訂雲標示。
 * 模組與索引圈都可用滑鼠或 Enter／空白鍵操作。
 */
export function BlueprintSheet(props: Props) {
  const [noteFor, setNoteFor] = useState<string | null>(null)
  const W = sheetWidth(props.cols)
  const zones = zoneCols(props.cols)
  const narrow = props.cols < 4
  const { placed, rows } = layout(props.modules, props.cols)
  const shownRows = Math.max(rows, 3)
  const modulesBottom = TOP + shownRows * ROW_H - 16
  // 窄圖紙放不下並排的修訂雲與標題欄：修訂雲移到標題欄上方
  const cloudTop = modulesBottom + 18
  const blockTop = narrow && props.openQuestions > 0 ? modulesBottom + 120 : modulesBottom
  const blockRows: StackRow[] = [
    { label: '圖名', value: fit(props.title, 13) },
    { label: '圖號', value: props.sheetNo },
    { label: '狀態', value: props.status },
    ...props.stack,
  ]
  const blockH = 30 + blockRows.length * 26
  const blockX = W - 44 - BLOCK_W
  const byName = new Map(placed.map((m) => [m.name, m]))
  const note = noteFor === null ? null : (byName.get(noteFor) ?? null)
  const noteH = note === null ? 0 : noteHeight(note, props.refSource)
  // 寬圖：說明框放在標題欄左側的空白（修訂雲下方）；窄圖：放在標題欄下方並把圖紙加高
  const noteX = narrow ? (W - NOTE_W) / 2 : 56
  const noteY = narrow ? blockTop + blockH + 20 : props.openQuestions > 0 ? cloudTop + 76 : blockTop + 10
  const H = Math.max(blockTop + blockH + 44, note === null ? 0 : noteY + noteH + 44)
  // 引線：從索引圈上緣往上，沿右側內緣往下，從標題欄（或說明框）下方繞到說明框，只走空白處
  let leader = ''
  if (note !== null) {
    const cx = note.x + BOX_W + 2
    const top = note.y - 12 - 14
    const edge = W - 30
    const noteCenter = noteX + NOTE_W / 2
    leader = narrow
      ? `M${cx} ${top} V${top - 6} H${edge} V${noteY - 10} H${noteCenter} V${noteY}`
      : `M${cx} ${top} V${top - 6} H${edge} V${Math.max(blockTop + blockH, noteY + noteH) + 12} H${noteCenter} V${noteY + noteH}`
  }
  const ghostCount = narrow ? 2 : 3

  return (
    <svg
      className="blueprint"
      viewBox={`0 0 ${W} ${H}`}
      role="group"
      aria-label="專案藍圖"
      onKeyDown={(e) => {
        if (e.key === 'Escape') setNoteFor(null)
      }}
    >
      <defs>
        <marker id="bp-tick" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="9" markerHeight="9" orient="auto">
          <path d="M2 8 L8 2" className="bp-tick" />
        </marker>
      </defs>
      <rect x="10" y="10" width={W - 20} height={H - 20} className="bp-frame-outer" />
      <rect x={10 + ZONE} y={10 + ZONE} width={W - 20 - ZONE * 2} height={H - 20 - ZONE * 2} className="bp-frame" />
      <g className="bp-zone-label" aria-hidden="true">
        {Array.from({ length: zones }, (_, i) => (
          <text key={`c${i}`} x={(W / zones) * (i + 0.5)} y={22} textAnchor="middle">
            {i + 1}
          </text>
        ))}
        {Array.from({ length: shownRows }, (_, i) => (
          <text key={`r${i}`} x={18} y={TOP + i * ROW_H + BOX_H / 2 + 4} textAnchor="middle">
            {ROW_LETTERS[i]}
          </text>
        ))}
      </g>

      {placed.length === 0 && (
        <g aria-hidden="true">
          {Array.from({ length: ghostCount }, (_, i) => (
            <rect
              key={i}
              x={W / 2 - (ghostCount * 220) / 2 + 30 + i * 220}
              y={TOP + ROW_H}
              width={160}
              height={BOX_H}
              className="bp-ghost"
            />
          ))}
          <text x={W / 2} y={TOP + ROW_H * 2 + 20} textAnchor="middle" className="bp-empty">
            {props.emptyText}
          </text>
        </g>
      )}

      <g className="bp-links" aria-hidden="true">
        {placed.flatMap((m) =>
          m.dependsOn
            .filter((d) => byName.has(d) && d !== m.name)
            .map((d, i) => {
              const dep = byName.get(d)!
              const x1 = m.x + BOX_W / 2 + (i - (m.dependsOn.length - 1) / 2) * 14
              const y1 = m.y + BOX_H
              const x2 = dep.x + BOX_W / 2
              const y2 = dep.y
              const path =
                y2 > y1
                  ? `M${x1} ${y1} V${y1 + (ROW_H - BOX_H) / 2} H${x2} V${y2}`
                  : `M${m.x + BOX_W} ${m.y + BOX_H / 2} C${m.x + BOX_W + 40} ${m.y + BOX_H / 2} ${dep.x - 40} ${dep.y + BOX_H / 2} ${dep.x} ${dep.y + BOX_H / 2}`
              return (
                <path
                  key={`${m.name}->${d}`}
                  d={path}
                  className={props.selected === m.name || props.selected === d ? 'bp-link bp-link-on' : 'bp-link'}
                  markerStart="url(#bp-tick)"
                  markerEnd="url(#bp-tick)"
                />
              )
            }),
        )}
      </g>

      {placed.map((m, index) => {
        const selected = props.selected === m.name
        const toggleNote = () => {
          props.onSelect(m.name)
          setNoteFor((current) => (current === m.name ? null : m.name))
        }
        return (
          <g key={m.name} className={selected ? 'bp-module bp-selected' : 'bp-module'} style={{ ['--i' as string]: index }}>
            <g
              className="bp-hit"
              role="button"
              tabIndex={0}
              aria-pressed={selected}
              aria-label={`${m.zone} ${m.name}：${m.purpose}`}
              onClick={() => props.onSelect(m.name)}
              onKeyDown={(e) => activate(e, () => props.onSelect(m.name))}
            >
              <title>{`${m.name}：${m.purpose}`}</title>
              <rect x={m.x} y={m.y} width={BOX_W} height={BOX_H} className="bp-box" />
              <text x={m.x + BOX_W / 2} y={m.y + 23} textAnchor="middle" className="bp-name">
                {fit(m.name, 10)}
              </text>
              <text x={m.x + BOX_W / 2} y={m.y + 41} textAnchor="middle" className="bp-purpose">
                {fit(m.purpose, 13)}
              </text>
            </g>
            {m.refs.length > 0 && (
              <g
                className="bp-callout"
                role="button"
                tabIndex={0}
                aria-expanded={noteFor === m.name}
                aria-label={`${m.name}：${m.refs.length} 處參考來源`}
                onClick={toggleNote}
                onKeyDown={(e) => activate(e, toggleNote)}
              >
                <circle cx={m.x + BOX_W + 2} cy={m.y - 12} r="14" />
                <text x={m.x + BOX_W + 2} y={m.y - 7} textAnchor="middle" className="bp-callout-top">
                  {m.refs.length}
                </text>
              </g>
            )}
          </g>
        )
      })}

      {props.openQuestions > 0 && (
        <g className="bp-revision">
          <path d={cloudPath(narrow ? W / 2 - 125 : 56, cloudTop, 250, 54)} />
          <text x={narrow ? W / 2 : 181} y={cloudTop + 24} textAnchor="middle" className="bp-revision-title">
            待確認 {props.openQuestions} 項
          </text>
          <text x={narrow ? W / 2 : 181} y={cloudTop + 42} textAnchor="middle" className="bp-revision-note">
            見藍圖說明
          </text>
        </g>
      )}

      <g className="bp-block">
        {placed.some((m) => m.refs.length > 0) && (
          <g className="bp-legend" aria-hidden="true">
            <circle cx={blockX + 14} cy={blockTop - 16} r="9" />
            <text x={blockX + 30} y={blockTop - 11}>
              數字＝參考來源處數，點圈在圖上查看
            </text>
          </g>
        )}
        <rect x={blockX} y={blockTop} width={BLOCK_W} height={blockH} />
        <text x={blockX + 12} y={blockTop + 20} className="bp-block-head">
          標題欄
        </text>
        {blockRows.map((row, i) => {
          const y = blockTop + 30 + i * 26
          const pending = row.value === null
          return (
            <g key={row.label}>
              <path d={`M${blockX} ${y} H${blockX + BLOCK_W}`} className="bp-block-rule" />
              {props.changedLabel === row.label && (
                <rect key={`${row.label}-${row.value}`} x={blockX + 1} y={y + 1} width={BLOCK_W - 2} height="24" className="bp-flash" />
              )}
              <text x={blockX + 12} y={y + 17} className="bp-block-label">
                {row.label}
              </text>
              <text x={blockX + 92} y={y + 17} className={pending ? 'bp-block-value bp-pending' : 'bp-block-value'}>
                {pending ? '待選' : fit(row.value ?? '', 17)}
              </text>
            </g>
          )
        })}
        <path d={`M${blockX + 84} ${blockTop + 30} V${blockTop + blockH}`} className="bp-block-rule" />
      </g>

      {note !== null && (
        <CalloutNote m={note} x={noteX} y={noteY} leader={leader} source={props.refSource} onClose={() => setNoteFor(null)} />
      )}
    </svg>
  )
}
