import { useCallback, useState } from 'react'
import { api, type ApiError } from './api'
import type { QaConsent, QaConsentList } from './types'
import { useLoad } from './useLoad'

export interface QaConsents {
  /** repo id → 同意時間（ISO）；沒有出現的 repo 就是未同意（預設）。第一次載入完成前為 null（狀態未知）。 */
  byRepo: ReadonlyMap<number, string> | null
  loading: boolean
  error: ApiError | null
  reload: () => void
  /** PUT／DELETE 成功後，把後端回的結果套用到本機狀態（列表只有一份，卡片與設定面板共用）。 */
  apply: (result: QaConsent) => void
  /** 同意（PUT，冪等：重複同意保留第一次的時間）。失敗會拋出 ApiError。 */
  grant: (repoId: number) => Promise<void>
  /** 撤回（DELETE，冪等）。失敗會拋出 ApiError。 */
  revoke: (repoId: number) => Promise<void>
}

/**
 * 逐 repo 的「資料外送同意」：一次請求讀取全部（GET /qa/consents），由 App 持有，
 * 所有 repo 卡片與設定面板的清單共用同一份，不是每張卡片各發一次請求。
 * 同意只是一個標記，這裡不送出任何程式內容。
 */
export function useQaConsents(): QaConsents {
  const [reloadKey, setReloadKey] = useState(0)
  const load = useLoad<QaConsentList>('/api/v1/qa/consents', reloadKey)
  const [seen, setSeen] = useState<QaConsentList | null>(null)
  const [byRepo, setByRepo] = useState<ReadonlyMap<number, string> | null>(null)

  if (load.data !== null && load.data !== seen) {
    setSeen(load.data)
    setByRepo(new Map(load.data.items.map((item) => [item.repository_id, item.consented_at])))
  }

  const reload = useCallback(() => setReloadKey((k) => k + 1), [])
  const apply = useCallback((result: QaConsent) => {
    // 函式型更新：兩張卡片幾乎同時操作時，不會用舊的 Map 蓋掉對方的結果
    setByRepo((current) => {
      const next = new Map(current ?? [])
      if (result.consented && result.consented_at !== null) next.set(result.repository_id, result.consented_at)
      else next.delete(result.repository_id)
      return next
    })
  }, [])

  const grant = useCallback(
    async (repoId: number) => {
      const res = await api<QaConsent>('PUT', `/api/v1/repositories/${repoId}/qa-consent`)
      apply(res.data)
    },
    [apply],
  )
  const revoke = useCallback(
    async (repoId: number) => {
      const res = await api<QaConsent>('DELETE', `/api/v1/repositories/${repoId}/qa-consent`)
      apply(res.data)
    },
    [apply],
  )

  return { byRepo, loading: load.loading, error: load.error, reload, apply, grant, revoke }
}
