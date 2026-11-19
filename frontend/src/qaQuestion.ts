// 問答顯示用的對照表與小函式（只負責「怎麼顯示」；陳述有沒有被採用、引用對不對都由後端決定）。

import type { QaContextSkipReason, QaQuestionStatus, QaStatementKind } from './types'

export const QA_STATUS_LABEL: Record<QaQuestionStatus, string> = {
  queued: '排隊中',
  running: '執行中',
  succeeded: '已完成',
  failed: '失敗',
  cancelled: '已取消',
}

// 徽章顏色沿用既有 .badge-*；文字永遠同時顯示
export const QA_STATUS_BADGE: Record<QaQuestionStatus, string> = {
  queued: 'badge-neutral',
  running: 'badge-importing',
  succeeded: 'badge-completed',
  failed: 'badge-failed',
  cancelled: 'badge-neutral',
}

export function isQaActive(status: QaQuestionStatus): boolean {
  return status === 'queued' || status === 'running'
}

/** 陳述種類：事實（有程式碼引用）／推測（有引用的推論）／待確認（證據不足、需要人確認）。 */
export const QA_KIND_LABEL: Record<QaStatementKind, string> = {
  fact: '事實',
  inference: '推測',
  open_question: '待確認',
}

export const QA_KIND_BADGE: Record<QaStatementKind, string> = {
  fact: 'badge-completed',
  inference: 'badge-importing',
  open_question: 'badge-neutral',
}

export const QA_KIND_ORDER: readonly QaStatementKind[] = ['fact', 'inference', 'open_question']

export const QA_KIND_NOTE: Record<QaStatementKind, string> = {
  fact: '每一項都附有已驗證的程式碼引用。',
  inference: '依引用推論而來，不是程式碼直接寫明的內容。',
  open_question: '證據不足或需要人再確認。',
}

/** 系統驗證引用時給出的原因（InvalidReason）。 */
export const QA_REJECTION_LABEL: Record<string, string> = {
  unknown_chunk_code: '引用的片段代號不在送出的片段內（模型自行編造）',
  line_out_of_range: '引用的行號超出該片段送出的範圍',
  citation_range_inverted: '引用的起訖行顛倒（起始行大於結束行）',
  fact_without_citation: '標為「事實」卻沒有任何引用',
  inference_without_citations: '標為「推測」卻沒有任何引用',
  statement_with_insufficient_evidence: '模型自己宣告證據不足，卻仍列出這項陳述',
}

export function rejectionLabel(reason: string): string {
  return QA_REJECTION_LABEL[reason] ?? `未知原因（${reason}）`
}

export const QA_SKIP_LABEL: Record<QaContextSkipReason, string> = {
  count_cap: '超過送出片段數量上限，未送出',
  budget_exhausted: '超過上下文大小預算，未送出',
  empty_text: '片段內容是空的，未送出',
}

export function skipLabel(reason: string): string {
  return QA_SKIP_LABEL[reason as QaContextSkipReason] ?? `未送出（${reason}）`
}

/** 引用位置：path:起–迄（只有一行時 path:行）。 */
export function lineRangeText(start: number, end: number): string {
  return start === end ? `${start}` : `${start}–${end}`
}

/** 耗時（毫秒）→ 「850 毫秒」「12.3 秒」「2 分 5 秒」。 */
export function durationText(ms: number | null): string {
  if (ms === null) return '—'
  if (ms < 1000) return `${ms} 毫秒`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)} 秒`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} 分 ${Math.round(seconds - minutes * 60)} 秒`
}

/** 檢視來源檔案時，在引用範圍前後多顯示幾行當作上下文。 */
export const SOURCE_CONTEXT_LINES = 5

/** 取出要顯示的行範圍（1 起算、含頭尾）；超出檔案的部分夾回檔案範圍。 */
export function sourceWindow(totalLines: number, start: number, end: number, context: number): { from: number; to: number } {
  const from = Math.max(1, Math.min(start, totalLines) - context)
  const to = Math.min(totalLines, Math.max(end, start) + context)
  return { from, to: Math.max(from, to) }
}
