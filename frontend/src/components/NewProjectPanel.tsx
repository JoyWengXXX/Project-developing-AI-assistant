import { useState } from 'react'
import type { Repository } from '../types'
import { ProjectInitForm } from './ProjectInitForm'
import { ProjectLlmChat } from './ProjectLlmChat'

interface Props {
  repos: Repository[]
  snapshotKey: number
}

/**
 * 「新專案」摺疊區（預設收合）：以參考 repo 為依據建立全新專案的入口。第一步是取得初始骨架的配置：
 * 已有本機骨架就匯入驗證；沒有就由對話引導選擇（LLM 只推薦）。兩個分頁都保持掛載，切換不會遺失輸入。
 */
export function NewProjectPanel({ repos, snapshotKey }: Props) {
  const [tab, setTab] = useState<'init' | 'llm'>('init')
  return (
    <details className="panel advanced">
      <summary>新專案（初始骨架）</summary>
      <div role="tablist" aria-label="初始骨架來源" className="tabs">
        <button
          type="button"
          role="tab"
          id="tab-project-init"
          aria-selected={tab === 'init'}
          aria-controls="tabpanel-project-init"
          className="tab"
          onClick={() => setTab('init')}
        >
          已有初始骨架
        </button>
        <button
          type="button"
          role="tab"
          id="tab-project-llm"
          aria-selected={tab === 'llm'}
          aria-controls="tabpanel-project-llm"
          className="tab"
          onClick={() => setTab('llm')}
        >
          沒有初始骨架（對話引導）
        </button>
      </div>
      <div role="tabpanel" id="tabpanel-project-init" aria-labelledby="tab-project-init" hidden={tab !== 'init'}>
        <ProjectInitForm />
      </div>
      <div role="tabpanel" id="tabpanel-project-llm" aria-labelledby="tab-project-llm" hidden={tab !== 'llm'}>
        <ProjectLlmChat repos={repos} snapshotKey={snapshotKey} />
      </div>
    </details>
  )
}
