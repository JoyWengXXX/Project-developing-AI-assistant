// 語言分布的純計算（不依賴 React、不 import 其他模組，方便單獨驗證）。
// 依「檔案數」計算百分比；顏色只是輔助，圖例文字（名稱、百分比、檔案數）才是主要資訊。

/** 後端固定用這個字串代表「不認得的副檔名」與超過上限的尾端語言。 */
export const OTHER_LANGUAGE = '其他'

export interface LanguageShare {
  language: string
  files: number
  bytes: number
  percent: number // 0–100，未四捨五入
  /** 具名語言的顏色序號（對應 CSS 變數循環）；「其他」為 null（用中性灰）。 */
  colorIndex: number | null
}

interface LanguageInput {
  language: string
  files: number
  bytes: number
}

/** 計算每個語言佔總檔案數的比例；「其他」排最後（後端已排好，這裡再保證一次）。總檔案數為 0 時回傳空陣列。 */
export function languageShares(languages: readonly LanguageInput[]): LanguageShare[] {
  const total = languages.reduce((sum, l) => sum + l.files, 0)
  if (total <= 0) return []
  const named = languages.filter((l) => l.language !== OTHER_LANGUAGE)
  const other = languages.filter((l) => l.language === OTHER_LANGUAGE)
  return [...named, ...other].map((l) => ({
    language: l.language,
    files: l.files,
    bytes: l.bytes,
    percent: (l.files / total) * 100,
    colorIndex: l.language === OTHER_LANGUAGE ? null : named.indexOf(l),
  }))
}

/** 百分比的顯示文字：≥10 取整數，其餘一位小數；極小值顯示 <0.1%。 */
export function formatPercent(percent: number): string {
  if (percent <= 0) return '0%'
  if (percent < 0.1) return '<0.1%'
  if (percent >= 10) return `${Math.round(percent)}%`
  return `${Math.round(percent * 10) / 10}%`
}
