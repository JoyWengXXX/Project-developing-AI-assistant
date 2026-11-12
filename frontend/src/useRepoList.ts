import { useState } from 'react'
import type { ApiError } from './api'
import type { Repository, RepositoryList } from './types'
import { useLoad } from './useLoad'

export interface RepoListState {
  /** 最近一次成功載入的列表；重新載入期間沿用舊列表（卡片不會被卸載重建，進行中的請求不會中斷）。 */
  items: Repository[] | null
  loading: boolean
  error: ApiError | null
  reload: () => void
  /** repo 已被移除：立刻從畫面上的列表拿掉（隨後的重新載入會與後端對齊）。 */
  dropLocal: (repoId: number) => void
}

/** 已註冊 repo 的列表（GET /api/v1/repositories），由 App 持有，讓總覽頁卸載再掛載時不閃爍。 */
export function useRepoList(): RepoListState {
  const [reloadKey, setReloadKey] = useState(0)
  const load = useLoad<RepositoryList>('/api/v1/repositories', reloadKey)
  const [seen, setSeen] = useState<RepositoryList | null>(null)
  const [items, setItems] = useState<Repository[] | null>(null)

  // 新結果到達：在 render 期間同步（不需要 effect）
  if (load.data !== null && load.data !== seen) {
    setSeen(load.data)
    setItems(load.data.items)
  }

  return {
    items,
    loading: load.loading,
    error: load.error,
    reload: () => setReloadKey((k) => k + 1),
    dropLocal: (repoId) => setItems((cur) => (cur === null ? cur : cur.filter((r) => r.id !== repoId))),
  }
}
