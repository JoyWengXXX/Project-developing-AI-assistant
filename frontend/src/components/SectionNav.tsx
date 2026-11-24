import type { KeyboardEvent } from 'react'
import type { Section } from '../navigation'

const SECTIONS: { id: Section; label: string }[] = [
  { id: 'project', label: '新專案' },
  { id: 'repos', label: '參考專案' },
  { id: 'qa', label: '問答' },
  { id: 'settings', label: '設定' },
]

/** 主導覽：四個分頁（左右方向鍵切換、Home／End 到頭尾）。分頁內容只隱藏不卸載，進行中的工作不會遺失。 */
export function SectionNav({ current, onChange }: { current: Section; onChange: (section: Section) => void }) {
  function onKeyDown(e: KeyboardEvent<HTMLButtonElement>, index: number) {
    let next = -1
    if (e.key === 'ArrowRight') next = (index + 1) % SECTIONS.length
    else if (e.key === 'ArrowLeft') next = (index - 1 + SECTIONS.length) % SECTIONS.length
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = SECTIONS.length - 1
    if (next < 0) return
    e.preventDefault()
    onChange(SECTIONS[next].id)
    document.getElementById(`section-tab-${SECTIONS[next].id}`)?.focus()
  }

  return (
    <nav className="section-nav" aria-label="主要功能">
      <div role="tablist" aria-label="主要功能">
        {SECTIONS.map((section, index) => (
          <button
            key={section.id}
            id={`section-tab-${section.id}`}
            type="button"
            role="tab"
            aria-selected={current === section.id}
            aria-controls={`section-${section.id}`}
            tabIndex={current === section.id ? 0 : -1}
            onClick={() => onChange(section.id)}
            onKeyDown={(e) => onKeyDown(e, index)}
          >
            {section.label}
          </button>
        ))}
      </div>
    </nav>
  )
}
