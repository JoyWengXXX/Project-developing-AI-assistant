import { formatBytes } from '../format'
import { formatPercent, languageShares } from '../languageStats'
import type { OverviewLanguage } from '../types'

function colorOf(colorIndex: number | null): string {
  // 沿用 commit 圖的 8 色變數（淺色／暗黑各有一組）；「其他」用中性灰（--muted）
  return colorIndex === null ? 'var(--muted)' : `var(--graph-${colorIndex % 8})`
}

/** 語言分布：水平分段條＋圖例。顏色只是輔助，圖例（名稱、百分比、檔案數）與 title 才是完整資訊。 */
export function LanguageBar({ languages }: { languages: OverviewLanguage[] }) {
  const shares = languageShares(languages)
  if (shares.length === 0) return <p className="muted">沒有可統計語言的檔案。</p>

  const summary = shares.map((s) => `${s.language} ${formatPercent(s.percent)}`).join('、')
  return (
    <div className="lang">
      <div className="lang-bar" role="img" aria-label={`語言分布（依檔案數）：${summary}`}>
        {shares.map((s) => (
          <span
            key={s.language}
            className="lang-seg"
            style={{ flexGrow: s.files, background: colorOf(s.colorIndex) }}
            title={`${s.language}：${formatPercent(s.percent)}，${s.files.toLocaleString('zh-TW')} 個檔案，${formatBytes(s.bytes)}`}
          />
        ))}
      </div>
      <ul className="lang-legend">
        {shares.map((s) => (
          <li key={s.language} title={`${s.language}：${s.files.toLocaleString('zh-TW')} 個檔案，${formatBytes(s.bytes)}`}>
            <span className="lang-swatch" style={{ background: colorOf(s.colorIndex) }} aria-hidden="true" />
            <span className="lang-name">{s.language}</span>
            <span className="lang-pct">{formatPercent(s.percent)}</span>
            <span className="muted">（{s.files.toLocaleString('zh-TW')} 個）</span>
          </li>
        ))}
      </ul>
      <p className="note lang-note">依副檔名與檔案數統計，不代表理解該語言的結構。</p>
    </div>
  )
}
