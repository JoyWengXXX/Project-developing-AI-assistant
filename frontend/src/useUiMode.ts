import { useEffect, useState } from 'react'

/** 一般模式：白話、隱藏 API 提示與進階工具；開發者模式：顯示全部面板與對應 API。 */
export type UiMode = 'general' | 'developer'

const STORAGE_KEY = 'repopilot-ui-mode'

function initialMode(): UiMode {
  return localStorage.getItem(STORAGE_KEY) === 'developer' ? 'developer' : 'general'
}

/** 介面模式：寫入 <html data-mode>（CSS 依此隱藏 API 提示）並記住選擇（只存在本機瀏覽器）。 */
export function useUiMode(): { mode: UiMode; setMode: (mode: UiMode) => void } {
  const [mode, setMode] = useState<UiMode>(initialMode)

  useEffect(() => {
    document.documentElement.dataset.mode = mode
    localStorage.setItem(STORAGE_KEY, mode)
  }, [mode])

  return { mode, setMode }
}
