import { useState } from 'react'
import type { ProjectConfig, Repository } from '../types'
import { ProjectInitForm } from './ProjectInitForm'
import { ProjectLlmChat } from './ProjectLlmChat'
import { ProjectPlanList } from './ProjectPlanList'
import { ProjectPlanPanel } from './ProjectPlanPanel'

interface Props {
  repos: Repository[]
  snapshotKey: number
}

/**
 * 「新專案」摺疊區（預設收合）：以參考 repo 為依據建立全新專案。流程：
 * ① 取得初始骨架的配置——已有本機骨架就匯入驗證；沒有就由對話引導選擇（LLM 只推薦）；
 * ② 以該配置規劃檔案清單（參考版本選填），確認後鎖定；③ 已保存的計畫可回頭查看與刪除。
 * 兩個分頁都保持掛載，切換不會遺失輸入。
 */
export function NewProjectPanel({ repos, snapshotKey }: Props) {
  const [tab, setTab] = useState<'init' | 'llm'>('init')
  const [config, setConfig] = useState<ProjectConfig | null>(null)
  const [plansKey, setPlansKey] = useState(0) // 計畫有變動（產生、確認、刪除）時遞增，清單重新載入
  const [deletedPlanId, setDeletedPlanId] = useState<number | null>(null)
  return (
    <details className="panel advanced">
      <summary>新專案（初始骨架與規劃）</summary>
      <h3>① 初始骨架</h3>
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
        <ProjectInitForm onDetected={(result) => setConfig(result.project_config)} />
      </div>
      <div role="tabpanel" id="tabpanel-project-llm" aria-labelledby="tab-project-llm" hidden={tab !== 'llm'}>
        <ProjectLlmChat repos={repos} snapshotKey={snapshotKey} onCompleted={setConfig} />
      </div>
      <h3>② 規劃新專案</h3>
      {config === null ? (
        <p className="empty">先完成上面的初始骨架（偵測或對話引導），才能規劃。</p>
      ) : (
        <ProjectPlanPanel
          key={JSON.stringify(config)}
          config={config}
          repos={repos}
          snapshotKey={snapshotKey}
          deletedPlanId={deletedPlanId}
          onChanged={() => setPlansKey((k) => k + 1)}
        />
      )}
      <h3>③ 已保存的計畫</h3>
      <ProjectPlanList
        reloadKey={plansKey}
        onDeleted={(id) => {
          setDeletedPlanId(id)
          setPlansKey((k) => k + 1)
        }}
      />
    </details>
  )
}
