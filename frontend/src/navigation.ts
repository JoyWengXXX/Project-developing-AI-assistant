// 主導覽的分頁。面板之間要跳頁時（例如問答面板的「前往問答設定」）發出事件，由 App 切換分頁，
// 避免把切換函式一路往下傳。

export type Section = 'project' | 'repos' | 'qa' | 'settings'

const EVENT = 'repopilot:navigate'

export function navigateTo(section: Section) {
  window.dispatchEvent(new CustomEvent<Section>(EVENT, { detail: section }))
}

export function onNavigate(handler: (section: Section) => void): () => void {
  const listener = (event: Event) => handler((event as CustomEvent<Section>).detail)
  window.addEventListener(EVENT, listener)
  return () => window.removeEventListener(EVENT, listener)
}
