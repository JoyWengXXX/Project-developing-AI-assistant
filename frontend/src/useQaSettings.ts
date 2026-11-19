import { useCallback, useState } from 'react'
import type { ApiError } from './api'
import type { QaSettings } from './types'
import { useLoad } from './useLoad'

export interface QaSettingsState {
  /** 最近一次成功載入（或儲存後回傳）的設定；重新載入期間沿用舊資料，第一次載入前為 null。 */
  settings: QaSettings | null
  loading: boolean
  error: ApiError | null
  reload: () => void
  /** 儲存類請求（PUT）回傳的完整設定：直接採用，不必再多讀一次。 */
  apply: (next: QaSettings) => void
}

/**
 * 問答設定（GET /qa/settings），由「問答設定」面板持有。
 * 回應永遠不含 API key（只有 provider_key_configured），所以這裡保存的資料可以安全放在 state。
 */
export function useQaSettings(): QaSettingsState {
  const [reloadKey, setReloadKey] = useState(0)
  const load = useLoad<QaSettings>('/api/v1/qa/settings', reloadKey)
  const [seen, setSeen] = useState<QaSettings | null>(null)
  const [settings, setSettings] = useState<QaSettings | null>(null)

  // 新結果到達：在 render 期間同步（與 useRepoList 相同的作法）
  if (load.data !== null && load.data !== seen) {
    setSeen(load.data)
    setSettings(load.data)
  }

  const reload = useCallback(() => setReloadKey((k) => k + 1), [])
  return { settings, loading: load.loading, error: load.error, reload, apply: setSettings }
}
