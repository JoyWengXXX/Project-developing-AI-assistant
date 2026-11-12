// 「已忽略的失敗工作」的純邏輯（不含 React，方便單獨驗證）。
// 忽略只是前端的顯示偏好：把工作編號記在瀏覽器的 localStorage，不會通知後端，也不改變工作本身。
// 只存編號陣列（不存工作內容、錯誤訊息或任何秘密）；讀到壞資料一律當成空的。

export const DISMISSED_JOBS_KEY = 'repopilot-dismissed-jobs'
/** 最多保留幾筆：超過時丟掉最舊的（陣列前面的），避免 localStorage 無限成長。 */
export const MAX_DISMISSED_JOBS = 200

/**
 * 解析 localStorage 的原始字串：必須是 JSON 陣列，只留下正整數編號，重複的只算一次，超過上限只留最後（最新）的。
 * 任何不合格的內容（null、壞 JSON、物件、含字串或負數的陣列）都容錯：不合格的項目略過，整體壞掉就回傳空陣列。
 */
export function parseDismissed(raw: string | null): number[] {
  if (raw === null) return []
  let value: unknown
  try {
    value = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(value)) return []
  return capDismissed(value.filter((v): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v > 0))
}

/** 去重（保留最後一次出現的位置）並只留最新的 MAX_DISMISSED_JOBS 筆。 */
function capDismissed(ids: readonly number[]): number[] {
  const unique = new Set<number>()
  for (const id of ids) {
    unique.delete(id)
    unique.add(id)
  }
  return [...unique].slice(-MAX_DISMISSED_JOBS)
}

/** 加入一個編號（已存在就移到最新），超過上限時丟掉最舊的。回傳新陣列，不修改原陣列。 */
export function addDismissed(ids: readonly number[], id: number): number[] {
  return capDismissed([...ids, id])
}

/** 移除一個編號（不存在就原樣回傳）。 */
export function removeDismissed(ids: readonly number[], id: number): number[] {
  return ids.includes(id) ? ids.filter((x) => x !== id) : [...ids]
}

type ReadableStorage = Pick<Storage, 'getItem'>
type WritableStorage = Pick<Storage, 'setItem'>

/** 讀取；瀏覽器禁用儲存空間（例如隱私設定）而拋例外時當成沒有資料。 */
export function readDismissed(storage: ReadableStorage | null): number[] {
  if (storage === null) return []
  try {
    return parseDismissed(storage.getItem(DISMISSED_JOBS_KEY))
  } catch {
    return []
  }
}

/** 寫入；失敗（空間已滿、被禁用）時只在這次瀏覽中有效，不拋例外。 */
export function writeDismissed(storage: WritableStorage | null, ids: readonly number[]): void {
  if (storage === null) return
  try {
    storage.setItem(DISMISSED_JOBS_KEY, JSON.stringify(ids))
  } catch {
    // 忽略：下次重新整理後會恢復顯示，不影響其他功能
  }
}
