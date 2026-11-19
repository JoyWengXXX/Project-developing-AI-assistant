import { useEffect, useState } from 'react'
import { AddRepoPanel } from './components/AddRepoPanel'
import { GitHubPanel } from './components/GitHubPanel'
import { GitHubRegistrations } from './components/GitHubRegistrations'
import { JobsSummaryBar } from './components/JobsSummaryBar'
import { QaSettingsPanel } from './components/QaSettingsPanel'
import { RepoDetailHeader } from './components/RepoDetailHeader'
import { RepoOverviewGrid } from './components/RepoOverviewGrid'
import { RetrievalPanel } from './components/RetrievalPanel'
import { StatusHeader } from './components/StatusHeader'
import { VersionPanel } from './components/VersionPanel'
import type { Repository } from './types'
import { useGitHubJobs } from './useGitHubJobs'
import { useJobsOverview } from './useJobsOverview'
import { useQaConsents } from './useQaConsents'
import { useRepoImports } from './useRepoImports'
import { useRepoList } from './useRepoList'

// 不使用路由或狀態管理函式庫：首頁是「倉庫總覽」（repo 卡片），點卡片後整頁切換成該 repo 的詳情
// （版本管理：已匯入的版本、commit 圖與選取 commit 的操作；檔案區），用「← 回到總覽」回來。選取狀態集中在這裡，用 props 往下傳。
// 選擇「切換」而不是「總覽下方再顯示詳情」：詳情很長，切換後不必捲動；總覽卸載時，
// 它的進行中請求（概況、已匯入版本）會一併取消；回到總覽時一律重新讀取（參考版本、概況都是最新的）。
// 匯入（註冊後的一次性匯入、卡片的「匯入」）的狀態由這裡的 useRepoImports 持有，不會因為換頁而中斷。
// 背景工作的資料也只在這裡輪詢一次（useJobsOverview）：頂端摘要、repo 卡片、詳情版本區與完整列表共用同一份，用 props 往下傳。
// GitHub 註冊與更新（fetch）也是背景工作，由這裡的 useGitHubJobs 送出並偵測完成（成功後重新載入列表、自動匯入一次、保存 refs 變更清單）。
// 面板上的 key={...} 讓「換 repo」時，版本區的分支選擇、commit 圖、檔案樹等內部狀態與進行中的請求自動重置。
// 問答的「資料外送同意」（逐 repo）也由這裡持有一份（useQaConsents，一次請求讀全部），repo 卡片與「問答設定」面板共用。
export default function App() {
  const [repo, setRepo] = useState<Repository | null>(null)
  const [returnedFromDetail, setReturnedFromDetail] = useState(false) // 回到總覽時把焦點移到標題
  const [focusImported, setFocusImported] = useState(false) // 從卡片的「管理已匯入版本」進來：焦點移到「已匯入的版本」
  const list = useRepoList()
  const imports = useRepoImports()
  const jobs = useJobsOverview(imports.busy)
  const consents = useQaConsents()
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
      <StatusHeader />
      <main className="layout">
        {/* 背景工作：頁面頂端只有一行摘要（含進入 repo 詳情時）；完整列表按「查看所有背景工作」才開啟。進行中與失敗的工作顯示在它所屬的 repo 卡片與詳情版本區 */}
        <JobsSummaryBar jobs={jobs} repos={list.items} />
        {/* 「GitHub 註冊工作」「新增 repo」與「GitHub 連線」：進入詳情時只是隱藏（不卸載），輸入內容與進行中的請求不會遺失。
            註冊成功前沒有 repo（沒有卡片），所以進行中與失敗的註冊工作列在這裡，不放在摺疊區內，重新整理頁面後也看得到 */}
        <div className="home-panels" hidden={repo !== null}>
          <GitHubRegistrations github={github} jobs={jobs} />
          <AddRepoPanel github={github} onRegistered={handleRegistered} />
          <GitHubPanel />
          <QaSettingsPanel repos={list.items} consents={consents} />
        </div>

        {repo === null ? (
          <>
            <RetrievalPanel repos={list.items ?? []} snapshotKey={snapshotRefreshKey} />
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
      </main>
    </>
  )
}
