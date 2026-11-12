import { useState } from 'react'
import type { ApiError } from './api'
import type { RepositoryOverview } from './types'
import { useLoad } from './useLoad'

export type OverviewPhase =
  | 'loading' // 第一次載入概況
  | 'error' // 概況載入失敗
  | 'empty' // 沒有 commit（空 repo，或找不到預設分支）：沒有東西可匯入
  | 'unanalyzed' // 預設分支最新版本還沒匯入：卡片顯示「尚未匯入」與「匯入」按鈕（不會自動匯入）
  | 'ready' // 預設分支最新版本已匯入，概況完整

export interface OverviewState {
  phase: OverviewPhase
  /** 最近一次成功取得的概況（重新載入期間沿用舊的，畫面不閃爍）。 */
  overview: RepositoryOverview | null
  /** 正在重新取得概況（GET）。 */
  refreshing: boolean
  loadError: ApiError | null
  /** 重新取得概況。 */
  reload: () => void
}

/**
 * 一張卡片的概況（GET /overview，唯讀）。**不會自動匯入**：未匯入時只告訴畫面 phase='unanalyzed'，
 * 由使用者按「匯入」（走 import-jobs，見 useRepoImports），或由註冊成功後的一次性匯入處理。
 * reloadKey：外部要求重新讀取（例如匯入完成）時遞增；與本地的 reload() 相加後仍單調遞增。
 */
export function useOverview(repoId: number, reloadKey: number): OverviewState {
  const [tick, setTick] = useState(0)
  const load = useLoad<RepositoryOverview>(`/api/v1/repositories/${repoId}/overview`, tick + reloadKey)

  // 重新載入期間沿用上一份概況（render 期間同步，不需要 effect）
  const [seen, setSeen] = useState<RepositoryOverview | null>(null)
  const [shown, setShown] = useState<RepositoryOverview | null>(null)
  if (load.data !== null && load.data !== seen) {
    setSeen(load.data)
    setShown(load.data)
  }

  let phase: OverviewPhase
  if (shown === null) phase = load.error !== null ? 'error' : 'loading'
  else if (shown.head === null) phase = 'empty'
  else if (shown.analyzed) phase = 'ready'
  else phase = 'unanalyzed'

  return {
    phase,
    overview: shown,
    refreshing: load.loading,
    loadError: load.error,
    reload: () => setTick((t) => t + 1),
  }
}
