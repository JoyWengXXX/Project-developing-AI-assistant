import { useState } from 'react'
import type { ProjectInfo, ProjectInfoKind } from '../types'

const KIND_LABEL: Record<ProjectInfoKind, string> = {
  pyproject: 'pyproject',
  requirements: 'requirements',
  package_json: 'package.json',
  csproj: 'csproj',
}

// 後端穩定代碼 → 簡短說明（沒有對照的代碼原樣顯示）
const PROBLEM_LABEL: Record<string, string> = {
  too_large: '檔案過大（超過 256 KiB），未讀取',
  invalid_toml: 'TOML 格式無法解析',
  invalid_json: 'JSON 格式無法解析',
  not_an_object: '內容不是 JSON 物件',
}

const PREVIEW_DEPENDENCIES = 5 // 每個專案預設列出幾個依賴
const PREVIEW_ITEMS = 3 // 預設顯示幾個專案檔

function displayName(info: ProjectInfo): string {
  if (info.name) return info.name
  const parts = info.path.split('/')
  return parts[parts.length - 1] ?? info.path
}

/** 一個專案檔：kind 徽章＋名稱（＋TargetFramework）＋前幾個依賴，可展開看全部。全部都是純文字輸出。 */
function ProjectInfoItem({ info }: { info: ProjectInfo }) {
  const [showAll, setShowAll] = useState(false)
  const problem = info.skipped_reason ?? info.parse_error
  const deps = showAll ? info.dependencies : info.dependencies.slice(0, PREVIEW_DEPENDENCIES)
  const hidden = info.dependencies.length - PREVIEW_DEPENDENCIES

  return (
    <li className="project-item">
      <div className="project-head" title={info.path}>
        <span className="badge badge-neutral">{KIND_LABEL[info.kind]}</span>
        <strong className="project-name">{displayName(info)}</strong>
        {info.version && <span className="muted">{info.version}</span>}
        {info.target_frameworks.map((tf) => (
          <span key={tf} className="badge badge-skip-info">
            {tf}
          </span>
        ))}
      </div>
      {problem !== null ? (
        <p className="muted project-problem">無法顯示依賴：{PROBLEM_LABEL[problem] ?? problem}</p>
      ) : info.dependencies.length === 0 ? (
        <p className="muted project-problem">沒有宣告依賴。</p>
      ) : (
        <>
          <ul className="dep-list">
            {deps.map((d, i) => (
              <li key={`${i}:${d}`}>
                <code>{d}</code>
              </li>
            ))}
          </ul>
          {hidden > 0 && (
            <button
              type="button"
              className="secondary small"
              aria-expanded={showAll}
              onClick={() => setShowAll(!showAll)}
            >
              {showAll ? '只顯示前幾個' : `顯示全部 ${info.dependencies.length} 個依賴`}
            </button>
          )}
          {info.truncated && showAll && <p className="note">依賴過多，後端只回傳前 {info.dependencies.length} 個。</p>}
        </>
      )}
    </li>
  )
}

/** 專案資訊區；沒有任何專案檔就不顯示（不推測）。 */
export function ProjectInfoBadges({ items }: { items: ProjectInfo[] }) {
  const [showAll, setShowAll] = useState(false)
  if (items.length === 0) return null
  const shown = showAll ? items : items.slice(0, PREVIEW_ITEMS)
  return (
    <div className="project-info">
      <h4>專案資訊</h4>
      <ul className="project-list">
        {shown.map((info) => (
          <ProjectInfoItem key={info.path} info={info} />
        ))}
      </ul>
      {items.length > PREVIEW_ITEMS && (
        <button type="button" className="secondary small" aria-expanded={showAll} onClick={() => setShowAll(!showAll)}>
          {showAll ? '收合專案列表' : `顯示全部 ${items.length} 個專案檔`}
        </button>
      )}
    </div>
  )
}
