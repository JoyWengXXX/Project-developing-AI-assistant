import { useEffect, useState } from 'react'
import { AddRepoPanel } from './components/AddRepoPanel'
import { AppHeader } from './components/AppHeader'
import { GitHubPanel } from './components/GitHubPanel'
import { GitHubRegistrations } from './components/GitHubRegistrations'
import { JobsSummaryBar } from './components/JobsSummaryBar'
import { ProjectStudio } from './components/ProjectStudio'
import { QaPanel } from './components/QaPanel'
import { QaSettingsPanel } from './components/QaSettingsPanel'
import { RepoDetailHeader } from './components/RepoDetailHeader'
import { RepoOverviewGrid } from './components/RepoOverviewGrid'
import { RetrievalPanel } from './components/RetrievalPanel'
import { SectionNav } from './components/SectionNav'
import { VersionPanel } from './components/VersionPanel'
import { onNavigate, type Section } from './navigation'
import type { Repository } from './types'
import { useGitHubJobs } from './useGitHubJobs'
import { useJobsOverview } from './useJobsOverview'
import { useQaConsents } from './useQaConsents'
import { useQaSettings } from './useQaSettings'
import { useRepoImports } from './useRepoImports'
import { useRepoList } from './useRepoList'
import { useUiMode } from './useUiMode'

// 不使用路由或狀態管理函式庫：頂端四個分頁（新專案、參考專案、問答、設定），只隱藏不卸載。
// 「參考專案」分頁內：總覽（repo 卡片）點卡片後切換成該 repo 的詳情（版本管理、檔案區），用「← 回到總覽」回來。
// 選取狀態集中在這裡，用 props 往下傳；回到總覽時一律重新讀取（參考版本、概況都是最新的）。
// 一般模式隱藏 API 提示與進階工具（檢索、偵測已有專案）；開發者模式全部顯示。
// 匯入（註冊後的一次性匯入、卡片的「匯入」）的狀態由這裡的 useRepoImports 持有，不會因為換頁而中斷。
// 背景工作的資料也只在這裡輪詢一次（useJobsOverview）：頂端摘要、repo 卡片、詳情版本區與完整列表共用同一份，用 props 往下傳。
// GitHub 註冊與更新（fetch）也是背景工作，由這裡的 useGitHubJobs 送出並偵測完成（成功後重新載入列表、自動匯入一次、保存 refs 變更清單）。
// 面板上的 key={...} 讓「換 repo」時，版本區的分支選擇、commit 圖、檔案樹等內部狀態與進行中的請求自動重置。
// 問答的「資料外送同意」（逐 repo）也由這裡持有一份（useQaConsents，一次請求讀全部），repo 卡片與「問答設定」面板共用。
// 問答設定（useQaSettings）同樣由這裡持有：「問答設定」面板與「問答」面板共用同一份（改了模型或 key，提問面板立刻跟著變）。
// 「問答」面板放在首頁面板區（進入 repo 詳情時只是隱藏、不卸載），進行中的問答不會因為換頁而遺失。
export default function App() {
  const ui = useUiMode()
  const [section, setSection] = useState<Section>('project')
  useEffect(() => onNavigate(setSection), [])
  const [repo, setRepo] = useState<Repository | null>(null)
  const [returnedFromDetail, setReturnedFromDetail] = useState(false) // 回到總覽時把焦點移到標題
  const [focusImported, setFocusImported] = useState(false) // 從卡片的「管理已匯入版本」進來：焦點移到「已匯入的版本」
  const list = useRepoList()
  const imports = useRepoImports()
  const jobs = useJobsOverview(imports.busy)
  const consents = useQaConsents()
  const qaSettings = useQaSettings()
  // repo 已註冊（本機註冊、GitHub 註冊的各種結果）：重新載入列表。只有新建的才自動匯入一次預設分支最新版本；
  // 已存在的不匯入。revision 用 HEAD：後端在提交時解析成完整 SHA。失敗時卡片會顯示錯誤與「重試匯入」，不會自動再試。
  function handleRegistered(repoId: number, created: boolean) {
    list.reload()
    if (created) imports.start(repoId, 'HEAD')
  }
  const github = useGitHubJobs(jobs, handleRegistered)
  // 每個 repo 匯入完成就立刻重抓背景工作（頂端的「進行中」數量不必等下一個輪詢週期）
  const finishedImports = Object.values(imports.doneTicks).reduce((sum, n) => sum + n, 0)
  const reloadJobs = jobs.reload
  useEffect(() => {
    if (finishedImports > 0) reloadJobs()
  }, [finishedImports, reloadJobs])
  // 快照有變動（匯入、移除、設定或取消參考）時遞增：版本區的 commits（imported 標記）與已匯入的版本清單都依此重新讀取
  const [snapshotRefreshKey, setSnapshotRefreshKey] = useState(0)

  function openRepo(r: Repository, options?: { focusImported?: boolean }) {
    setRepo(r)
    setReturnedFromDetail(false)
    setFocusImported(options?.focusImported === true)
    window.scrollTo(0, 0)
  }

  function backToOverview() {
    setRepo(null)
    setReturnedFromDetail(true)
    list.reload() // 回來時順便與後端對齊列表
  }

  function bumpSnapshots() {
    setSnapshotRefreshKey((k) => k + 1)
  }

  // repo 已被移除（或已不存在）：若它正是目前選取的 repo，就清除選取，
  // 版本／檔案區隨之卸載（進行中的 GET 由 useLoad 取消，匯入由 useVersionLoad 取消，批次移除由 useBatchRemoval 停止），不會再對它發請求。
  // 用函式型更新：移除要花數秒，期間使用者可能已進入別的 repo，不能用舊的 repo 判斷。
  function handleRepoRemoved(removedId: number) {
    setRepo((current) => (current?.id === removedId ? null : current))
  }

  return (
    <>
      <a className="skip-link" href="#main">
        跳到主要內容
      </a>
      <AppHeader mode={ui.mode} onModeChange={ui.setMode} />
      <SectionNav current={section} onChange={setSection} />
      <main className="layout" id="main">
        {/* 背景工作摘要只在開發者模式顯示；一般模式下進行中與失敗的工作仍顯示在所屬的 repo 卡片與詳情 */}
        {ui.mode === 'developer' && <JobsSummaryBar jobs={jobs} repos={list.items} />}

        {/* 四個分頁只隱藏不卸載：輸入內容、進行中的對話、問答與請求不會因為換頁而遺失 */}
        <section id="section-project" role="tabpanel" aria-labelledby="section-tab-project" hidden={section !== 'project'}>
          <ProjectStudio repos={list.items ?? []} snapshotKey={snapshotRefreshKey} settings={qaSettings} mode={ui.mode} />
        </section>

        <section
          id="section-repos"
          role="tabpanel"
          aria-labelledby="section-tab-repos"
          hidden={section !== 'repos'}
          className="section-stack"
        >
          {/* 註冊成功前沒有 repo（沒有卡片），所以進行中與失敗的註冊工作列在這裡；進入詳情時只是隱藏 */}
          <div className="home-panels" hidden={repo !== null}>
            <GitHubRegistrations github={github} jobs={jobs} />
            <AddRepoPanel github={github} onRegistered={handleRegistered} />
          </div>
          {repo === null ? (
            <>
              {ui.mode === 'developer' && <RetrievalPanel repos={list.items ?? []} snapshotKey={snapshotRefreshKey} />}
              <RepoOverviewGrid
                list={list}
                imports={imports}
                jobs={jobs}
                github={github}
                consents={consents}
                onOpen={openRepo}
                onRemoved={handleRepoRemoved}
                focusTitle={returnedFromDetail}
              />
            </>
          ) : (
            <>
              <RepoDetailHeader key={`header-${repo.id}`} repo={repo} onBack={backToOverview} />
              <VersionPanel
                key={`version-${repo.id}`}
                repo={repo}
                jobs={jobs}
                reloadKey={github.fetchTicks[repo.id] ?? 0}
                snapshotKey={snapshotRefreshKey}
                onSnapshotsChanged={bumpSnapshots}
                focusImported={focusImported}
              />
            </>
          )}
        </section>

        <section id="section-qa" role="tabpanel" aria-labelledby="section-tab-qa" hidden={section !== 'qa'}>
          <QaPanel
            repos={list.items ?? []}
            consents={consents}
            settings={qaSettings}
            jobs={jobs}
            snapshotKey={snapshotRefreshKey}
            defaultOpen
          />
        </section>

        <section
          id="section-settings"
          role="tabpanel"
          aria-labelledby="section-tab-settings"
          hidden={section !== 'settings'}
          className="section-stack"
        >
          <QaSettingsPanel repos={list.items} consents={consents} state={qaSettings} defaultOpen />
          <GitHubPanel />
        </section>
      </main>
    </>
  )
}
