// 問答的錯誤訊息（繁體中文）：把後端穩定的錯誤碼（提問時的 HTTP 錯誤，或問答失敗後記錄的 error.code）對應成白話說明。
// 這裡只負責「怎麼說」：額度夠不夠、同意了沒有、引用對不對都由後端決定，前端不重做判斷。
// 純函式、只匯入型別與 qaFormat（它也只匯入型別），讓 node --test 可以直接載入。

import { formatTokens, formatUsd } from './qaFormat.ts'
import type { ErrorDetails } from './types'

/** 錯誤碼 → 靜態說明（不含動態數字）。也併入 format.ts 的 ERROR_HINT，讓背景工作列表看到同樣的白話說明。 */
export const QA_ERROR_HINT: Record<string, string> = {
  // 提問時（HTTP 錯誤）
  retrieval_index_not_ready: '有選取的版本還沒有可用的檢索索引（尚未建立、建立中或失敗）；請到「跨 repo 檢索與搜尋比較」建立後再提問。',
  qa_model_not_selected: '尚未選擇問答模型；請在「問答設定」選擇低、中、高階其中一個並儲存。',
  qa_provider_not_configured: '沒有可用的供應商 API key（未設定，或已保存但無法解密）；請在「問答設定」貼上 key。沒有任何內容被送出。',
  qa_egress_not_consented: '有 repo 尚未同意把程式片段送往供應商；請到該 repo 的卡片（或「問答設定」的同意清單）同意後再提問。沒有任何內容被送出。',
  token_budget_exceeded: 'RepoPilot 自己設定的 token／費用額度不允許這次請求（不是供應商的限制）；可在「問答設定」調整額度，沒有呼叫供應商。',
  qa_question_not_found: '這筆問答已不存在（可能已被刪除）。',
  qa_question_active: '這筆問答還在排隊或執行中，不能刪除；請先取消，或等它結束。',
  // 問答失敗（記錄在問答的 error.code）
  provider_max_tokens: '模型在輸出上限（max_tokens）停下，答案不完整，所以沒有採用。可以重新提問；用量仍會計入。',
  provider_refusal: '模型拒絕回答這個問題。',
  provider_context_window_exceeded: '送出的內容超過模型的上下文視窗；請減少選取的版本或縮短問題。',
  provider_timeout: '供應商沒有在時限內回應。RepoPilot 已自動重試過，仍然失敗；請稍後再提問。',
  provider_rate_limited: '供應商限流（請求太頻繁）。RepoPilot 已自動重試過，仍然失敗；請稍後再提問。',
  provider_unavailable: '供應商暫時無法使用或過載。RepoPilot 已自動重試過，仍然失敗；請稍後再提問。',
  provider_payment_required: '供應商帳號餘額不足或需要付款：請到 Anthropic Console 的 Plans & Billing 購買額度（API 與 Claude 訂閱的額度是分開的；這不是 RepoPilot 的額度）。',
  provider_request_too_large: '請求對供應商來說太大；請減少選取的版本或縮短問題。',
  provider_malformed_response: '供應商回傳了無法使用的回應。可以重新提問。',
  provider_auth_failed: '供應商拒絕了這把 API key（無效、過期或已撤銷）；請到「問答設定」重新貼上有效的 key。',
  provider_bad_request: '供應商認為這個請求不合法（通常是程式或模型設定問題）；請用 request_id 查後端日誌。',
  provider_spend_limit_reached:
    '供應商端（Anthropic 組織）的花費上限已用完。這是「供應商那邊」的限制，不是 RepoPilot 的 token 額度；請到供應商主控台調高上限，或等它重置。',
  provider_error: '呼叫供應商失敗（原因不明）；請用 request_id 查後端日誌。',
  qa_model_unsupported: '選取的模型不受支援；請到「問答設定」改選其他模型。',
  answer_parse_error: '模型的回答不符合要求的格式，所以沒有採用。可以重新提問。',
  answer_too_large: '模型的回答太大，所以沒有採用。可以重新提問。',
  answer_not_json: '模型的回答不是有效的 JSON，所以沒有採用。可以重新提問。',
  answer_not_object: '模型的回答格式不對（不是物件），所以沒有採用。可以重新提問。',
  answer_unexpected_fields: '模型的回答含有不允許的欄位，所以沒有採用。可以重新提問。',
  answer_invalid_field: '模型的回答有缺少或不合法的欄位，所以沒有採用。可以重新提問。',
  answer_empty_statements: '模型說「已回答」卻沒有任何陳述，所以沒有採用。可以重新提問。',
}

const BUDGET_REASON_LABEL: Record<string, string> = {
  per_request_tokens: '單次請求 token 上限',
  daily_tokens: '每日 token 上限',
  daily_cost_usd: '每日費用上限',
}

export interface QaErrorInput {
  code: string
  message: string
  details?: ErrorDetails
}

export interface QaErrorText {
  /** 主要說明（一句到兩句）。 */
  summary: string
  /** 下一步建議（沒有就是 null）。 */
  advice: string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** details 是不可信內容：只接受數字或字串。 */
function scalar(value: unknown): string | number | null {
  return typeof value === 'number' || typeof value === 'string' ? value : null
}

function amount(reason: string, value: string | number | null): string {
  if (value === null) return '（未知）'
  if (reason === 'daily_cost_usd') return `${formatUsd(String(value))} 美元`
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? `${formatTokens(n)} token` : String(value)
}

/** token_budget_exceeded：提問時有 details；問答失敗後只有訊息裡的括號（reason），沒有數字。 */
function budgetText(error: QaErrorInput): QaErrorText {
  const details = isRecord(error.details) ? error.details : {}
  let reason = typeof details.reason === 'string' ? details.reason : null
  if (reason === null) {
    const match = /\((per_request_tokens|daily_tokens|daily_cost_usd)\)/.exec(error.message)
    reason = match?.[1] ?? null
  }
  const label = reason === null ? null : (BUDGET_REASON_LABEL[reason] ?? null)
  const limit = scalar(details.limit)
  const requested = scalar(details.requested)
  const used = scalar(details.used)
  let summary = 'RepoPilot 的額度不允許這次請求（這是 RepoPilot 自己設定的額度，不是供應商的限制），沒有呼叫供應商。'
  if (reason !== null && label !== null) {
    summary = `超過 RepoPilot 的「${label}」（這是 RepoPilot 自己設定的額度，不是供應商的限制），沒有呼叫供應商。`
    const parts: string[] = []
    if (limit !== null) parts.push(`上限 ${amount(reason, limit)}`)
    if (used !== null) parts.push(`今日已用 ${amount(reason, used)}`)
    if (requested !== null) parts.push(`這次需要 ${amount(reason, requested)}`)
    if (parts.length > 0) summary += `（${parts.join('、')}）`
  }
  return { summary, advice: '可到「問答設定」的「額度」調高上限、等明天（或手動重置今日用量），或縮短問題／減少選取的版本。' }
}

function consentText(error: QaErrorInput): QaErrorText {
  const details = isRecord(error.details) ? error.details : {}
  const repos = Array.isArray(details.repositories) ? details.repositories : []
  const names = repos
    .map((item) => (isRecord(item) && typeof item.name === 'string' ? item.name : null))
    .filter((name): name is string => name !== null)
  const which = names.length > 0 ? names.map((name) => `「${name}」`).join('、') : '部分已選取的 repo'
  return {
    summary: `${which} 尚未同意把程式片段送往供應商（Anthropic API），所以這次提問被拒絕，沒有任何內容被送出。`,
    advice: '請回到「倉庫總覽」，在該 repo 卡片的「問答資料外送」區按「同意」（也可在「問答設定」的同意清單操作），再重新提問。',
  }
}

function indexNotReadyText(error: QaErrorInput): QaErrorText {
  const details = isRecord(error.details) ? error.details : {}
  const id = scalar(details.snapshot_id)
  const status = typeof details.index_status === 'string' ? details.index_status : null
  return {
    summary: `${id !== null ? `快照 #${id}` : '有選取的快照'}還沒有可用的檢索索引${status !== null ? `（狀態：${status}）` : ''}。`,
    advice: '請到「跨 repo 檢索與搜尋比較」為它建立檢索索引，完成後再提問。',
  }
}

/** 把問答的錯誤（HTTP 錯誤或問答失敗的 error）轉成繁體中文說明；不認得的錯誤碼退回後端訊息。 */
export function qaErrorText(error: QaErrorInput): QaErrorText {
  switch (error.code) {
    case 'token_budget_exceeded':
      return budgetText(error)
    case 'qa_egress_not_consented':
      return consentText(error)
    case 'retrieval_index_not_ready':
      return indexNotReadyText(error)
    case 'snapshot_not_found': {
      const id = isRecord(error.details) ? scalar(error.details.snapshot_id) : null
      return { summary: `${id !== null ? `快照 #${id}` : '有選取的快照'}已不存在（可能已被移除）。`, advice: '請重新選擇版本。' }
    }
    case 'snapshot_not_completed': {
      const id = isRecord(error.details) ? scalar(error.details.snapshot_id) : null
      return { summary: `${id !== null ? `快照 #${id}` : '有選取的快照'}還沒有匯入完成，不能用來提問。`, advice: '請等它匯入完成，或改選其他版本。' }
    }
    case 'search_scope_too_large':
      return {
        summary: '選取的版本片段總數太多，無法檢索；沒有呼叫供應商。',
        advice: '請減少選取的版本後再提問。',
      }
    case 'validation_error':
      return { summary: '送出的內容不合法（問題需 1～2000 字，且至少選一個、最多 20 個不重複的版本）。', advice: null }
    case 'network_error':
      return { summary: '無法連線到伺服器；問題沒有送出。', advice: '請確認後端仍在執行後再試。' }
    default: {
      const hint = QA_ERROR_HINT[error.code]
      if (hint !== undefined) return { summary: hint, advice: null }
      return { summary: error.message || error.code, advice: null }
    }
  }
}
