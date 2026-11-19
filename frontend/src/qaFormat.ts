// 問答設定的顯示與輸入檢查。金額一律以後端給的字串處理（不轉成浮點數），只做字串層級的整理。

import type { QaTier } from './types'

export const TIER_LABEL: Record<QaTier, string> = { low: '低', mid: '中', high: '高' }

/**
 * 後端 Decimal 字串可能是指數表示（例如零會是 "0E-8"）：展開成一般的小數字串（只做字串運算，不經過浮點數）。
 */
export function plainDecimal(value: string): string {
  const match = /^(-?)(\d+)(?:\.(\d*))?[eE]([+-]?\d+)$/.exec(value.trim())
  if (match === null) return value.trim()
  const [, sign = '', whole = '', fraction = '', exponent = '0'] = match
  const digits = whole + fraction
  const point = whole.length + Number(exponent)
  let intPart: string
  let fracPart: string
  if (point <= 0) {
    intPart = '0'
    fracPart = '0'.repeat(-point) + digits
  } else if (point >= digits.length) {
    intPart = digits + '0'.repeat(point - digits.length)
    fracPart = ''
  } else {
    intPart = digits.slice(0, point)
    fracPart = digits.slice(point)
  }
  intPart = intPart.replace(/^0+(?=\d)/, '')
  return `${sign}${intPart}${fracPart ? `.${fracPart}` : ''}`
}

/** 後端 Decimal 字串 → 顯示用：去掉多餘的尾端 0，但至少保留 2 位小數（0.10000000 → 0.10；2 → 2.00；0E-8 → 0.00）。 */
export function formatUsd(value: string): string {
  const [whole, fraction = ''] = plainDecimal(value).split('.')
  const trimmed = fraction.replace(/0+$/, '')
  return `$${whole}.${trimmed.padEnd(2, '0')}`
}

export function formatTokens(value: number): string {
  return value.toLocaleString('zh-TW')
}

// ---------- 輸入檢查（後端仍是最終依據；這裡只是讓明顯的錯誤不必送出） ----------

export type Parsed<T> = { ok: true; value: T } | { ok: false; message: string }

const INT32_MAX = 2_147_483_647

/** 空白＝不限制（null）；否則必須是正整數。 */
export function parseTokenLimit(text: string, max: number): Parsed<number | null> {
  const s = text.trim()
  if (s === '') return { ok: true, value: null }
  if (!/^\d+$/.test(s)) return { ok: false, message: '請輸入正整數，或留空代表不限制。' }
  const n = Number(s)
  if (n < 1 || n > max) return { ok: false, message: `必須介於 1 到 ${max.toLocaleString('zh-TW')}。` }
  return { ok: true, value: n }
}

export const PER_REQUEST_MAX = INT32_MAX
export const DAILY_TOKENS_MAX = Number.MAX_SAFE_INTEGER

/** 空白＝不限制（null）；否則是大於 0、最多 8 位小數的美元金額（保持字串送出）。 */
export function parseCostLimit(text: string): Parsed<string | null> {
  const s = text.trim()
  if (s === '') return { ok: true, value: null }
  if (!/^\d{1,10}(\.\d{1,8})?$/.test(s)) {
    return { ok: false, message: '請輸入美元金額（最多 8 位小數），或留空代表不限制。' }
  }
  if (!/[1-9]/.test(s)) return { ok: false, message: '必須大於 0；不限制請留空。' }
  return { ok: true, value: s }
}

/** 單價：大於或等於 0、最多 6 位小數的美元金額（每百萬 token）。 */
export function parsePrice(text: string): Parsed<string> {
  const s = text.trim()
  if (!/^\d{1,6}(\.\d{1,6})?$/.test(s)) return { ok: false, message: '請輸入非負的美元金額（最多 6 位小數）。' }
  return { ok: true, value: s }
}

/** 「欄位文字」與「已儲存值」是否代表同一個數字（以字串比較，不做浮點運算）。 */
export function sameDecimal(a: string, b: string): boolean {
  const norm = (v: string) => {
    const [whole, fraction = ''] = plainDecimal(v).split('.')
    const f = fraction.replace(/0+$/, '')
    return `${whole.replace(/^0+(?=\d)/, '')}${f ? `.${f}` : ''}`
  }
  return norm(a) === norm(b)
}
