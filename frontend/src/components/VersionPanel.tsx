import { useCallback, useEffect, useMemo, useState } from 'react'
import { refsBySha } from '../importedList'
import { jobCommitSha } from '../jobsOverview'
import { repoTitle } from '../format'
import type { Repository, RefsResponse } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useCommits } from '../useCommits'
import { useOnFalling, type JobsOverview } from '../useJobsOverview'
import { useLoad } from '../useLoad'
import { useSnapshotList } from '../useSnapshotList'
import { useVersionLoad } from '../useVersionLoad'
import { ALL_BRANCHES, isEmptyRepo, resolveScope } from '../versionScope'
import { BranchSelect } from './BranchSelect'
import { CommitDetail, VersionSummary } from './CommitDetail'
import { CommitGraph } from './CommitGraph'
import { FilePanel } from './FilePanel'
import { ImportedVersions } from './ImportedVersions'
import { RepoJobNotice } from './RepoJobNotice'
import { VersionActions } from './VersionActions'
import { VersionLoadStatus } from './VersionLoadStatus'

interface Props {
  repo: Repository
  /** 頁面共用的背景工作輪詢（App 持有）：版本區取這個 repo 的進行中與失敗工作，不另外發請求；也用它的 /jobs/status 決定匯入方式。 */
  jobs: JobsOverview
  /** GitHub 來源 fetch 成功後遞增：重新載入分支與 commit 圖。 */
  reloadKey: number
  /** 快照有變動（匯入、移除、設定參考）後遞增：重新讀取 commits 的 imported 標記與已匯入的版本。 */
  snapshotKey: number
  /** 這個面板造成快照變動（匯入完成、設定／取消參考、移除版本）後通知父元件遞增 snapshotKey。 */
  onSnapshotsChanged: () => void
  /** 從總覽的「管理已匯入版本」進來：把焦點移到「已匯入的版本」區塊。 */
  focusImported: boolean
}

/**
 * 版本管理：選了 repo 就自動載入分支清單（refs）、commit 圖與已匯入的版本。版面由上而下：
 *   1. 分支下拉
 *   2. 「選取的 commit」操作列（匯入／設為參考／移除此版本／進度與錯誤）——在圖的上方，寬螢幕捲動時固定在視窗頂端
 *   3. 左：已匯入的版本（勾選、批次移除）；右：commit 圖（窄螢幕上下排列）
 * 點選 commit 只顯示它的資訊，**不會自動匯入**——按「匯入此版本」才匯入（背景工作＋進度）。
 *
 * 狀態只有兩個是「使用者的選擇」，其餘都是由資料推導出來的：
 *   - choice：下拉選的值（null＝還沒選過，用預設分支）。
 *   - picked：使用者點選的 commit（null＝跟隨目前範圍的最新 commit）。
 * 這樣選了別的分支、fetch 讓分支消失、重新整理，都不需要「同步」多份狀態。
 *
 * 「已匯入」的判斷：已匯入版本列表（GET /snapshots，唯讀，取得快照編號用）有這個 commit，
 * 或本次匯入成功；列表還沒載入時退而用 commits 回應的 imported 欄位。不為了取得快照編號而再送匯入請求
 * （import-jobs 回 200 reused 雖然不產生新資料，但資料若剛好在別處被移除就可能重新匯入，風險不值得）。
 * App 以 key={repo.id} 掛載：換 repo 時整個面板（含進行中的請求）重來。
 */
export function VersionPanel({ repo, jobs, reloadKey, snapshotKey, onSnapshotsChanged, focusImported }: Props) {
  const [localTick, setLocalTick] = useState(0) // 按「重新整理」遞增；與 reloadKey 相加後仍單調遞增
  const tick = reloadKey + localTick
  const refs = useLoad<RefsResponse>(`/api/v1/repositories/${repo.id}/refs`, tick)
  const [choice, setChoice] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [reveal, setReveal] = useState<{ sha: string; seq: number } | null>(null) // 要求 commit 圖把某個 commit 捲進可見範圍
  const [filesOpen, setFilesOpen] = useState(false) // 檔案區是否展開（預設收合）
  const [removedNotice, setRemovedNotice] = useState<{ sha: string; text: string } | null>(null)

  // 重新整理 refs 的期間（useLoad 會暫時回 null）保留上一份，讓下拉與檔案區不閃爍、檔案樹的展開／選取狀態不被重置
  const [shownRefs, setShownRefs] = useState<RefsResponse | null>(null)
  if (refs.data !== null && refs.data !== shownRefs) setShownRefs(refs.data)

  const refsData = shownRefs
  const empty = refsData !== null && isEmptyRepo(refsData)
  const scope = refsData !== null ? resolveScope(refsData, choice) : null
  // 先有（最新的）refs 才知道預設分支是哪個，所以 commits 要等 refs（避免先載入「所有分支」再切換，
  // 也避免用已被 fetch 移除的舊分支名稱查詢）。snapshotKey 只重新讀取 commits（不動 refs），圖不會閃掉。
  const commits = useCommits(
    repo.id,
    refs.data === null || empty || scope === null ? undefined : scope.ref,
    tick + snapshotKey,
  )

  const selectedSha = empty ? null : (picked ?? scope?.tipSha ?? null)
  // 離開被移除版本的畫面後，清掉移除的提示（也避免之後回到它時又把焦點搶到按鈕上）
  if (removedNotice !== null && removedNotice.sha !== selectedSha) setRemovedNotice(null)
  const version = useVersionLoad(repo.id, selectedSha, onSnapshotsChanged, jobs)
  // 全部狀態的快照（失敗、過期設定也要在「已匯入的版本」清單看到）；「已匯入」的判斷只看 completedBySha
  const imported = useSnapshotList(repo.id, '', snapshotKey)
  const selectedCommit = commits.items?.find((c) => c.sha === selectedSha) ?? null
  const defaultBranch = refsData?.branches.find((b) => b.is_default)?.name ?? null

  // 這個 repo 的背景工作（共用輪詢）：對應「目前選取的 commit」的進行中工作併入匯入區的進度（不另外再顯示一份）；
  // 其他版本的進行中工作與最近失敗顯示在匯入區上方的一行提示
  const repoJobs = jobs.forRepo(repo.id)
  const selectedKey = selectedSha?.toLowerCase() ?? null
  const externalJob = selectedKey === null ? null : (repoJobs.active.find((j) => jobCommitSha(j) === selectedKey) ?? null)
  // 背景工作全部結束（例如重新整理頁面前就開始的匯入完成了）：重新讀取已匯入的版本。
  // 這次操作自己（useVersionLoad 追蹤的）送出的工作，結束時已由 onLoaded 處理，不重複
  useOnFalling(
    repoJobs.active.some((j) => j.id !== version.job?.id),
    onSnapshotsChanged,
  )
  const completedSnapshots = useMemo(() => imported.items?.filter((s) => s.status === 'completed') ?? null, [imported.items])
  // commit 圖上「匯入中」徽章：這次操作的，加上共用輪詢看到的（選取 commit 的）進行中匯入
  const loadingShas = useMemo(
    () => (externalJob === null || selectedSha === null ? version.loadingShas : new Set([...version.loadingShas, selectedSha])),
    [version.loadingShas, externalJob, selectedSha],
  )

  // 已匯入版本列表載入完成後，把本次瀏覽中記住、但列表已沒有的快照忘掉（例如在清單中移除了它）
  const { forget, loadedShas } = version
  const { items: importedItems, loading: importedLoading, completedBySha } = imported
  useEffect(() => {
    if (importedItems === null || importedLoading) return
    for (const loadedSha of loadedShas) if (!completedBySha.has(loadedSha)) forget(loadedSha)
  }, [importedItems, importedLoading, completedBySha, loadedShas, forget])

  const listSnapshot = selectedSha !== null ? (completedBySha.get(selectedSha) ?? null) : null
  const snapshot = version.snapshot ?? listSnapshot
  const isImported = selectedSha !== null && (snapshot !== null || (importedItems === null && selectedCommit?.imported === true))

  // 清單每一列的分支／標籤徽章與 commit 訊息：由已載入的 refs、commits 對應（對不上就不顯示）
  const refBadges = useMemo(() => refsBySha(refsData), [refsData])
  const subjectBySha = useMemo(() => new Map((commits.items ?? []).map((c) => [c.sha, c.subject])), [commits.items])

  // 點清單的一列：選取該 commit，並把它在圖中捲到可見範圍（還沒載入到圖中就只顯示資訊，不強制載入更多）
  const pickFromList = useCallback((sha: string) => {
    setPicked(sha)
    setReveal((prev) => ({ sha, seq: (prev?.seq ?? 0) + 1 }))
  }, [])

  // 「對應 API」：匯入用背景工作（import-jobs＋輪詢）；worker 不可用時回退的同步 /imports 已棄用
  const loadApis =
    version.method?.kind === 'sync'
      ? [`POST /api/v1/repositories/${repo.id}/imports（deprecated：worker 不可用時的同步回退）`]
      : [
          `POST /api/v1/repositories/${repo.id}/import-jobs（按「匯入此版本」時提交；已匯入的版本回 200 直接使用）`,
          'GET /api/v1/jobs/{id}（輪詢進度）',
          'GET /api/v1/snapshots/{id}（完成後取得快照）',
          'POST /api/v1/jobs/{id}/cancel（「取消匯入」：排隊中立即取消 200；執行中回 202，在下一批之間停止；409 job_not_cancellable＝已結束，改為重新讀取結果）',
          `POST /api/v1/repositories/${repo.id}/import-jobs（取消後的「重新匯入此版本」：同一個 job id 重新排入）`,
        ]

  function chooseScope(value: string) {
    setChoice(value)
    // 選分支／標籤：自動選取它的最新 commit；選「所有分支」：保持目前選取的 commit 不變
    setPicked(value === ALL_BRANCHES ? selectedSha : null)
  }

  function handleRemoved(sha: string, text: string) {
    version.forget(sha)
    setRemovedNotice({ sha, text })
    onSnapshotsChanged()
  }

  const summary =
    selectedSha !== null && scope !== null ? (
      <VersionSummary scopeLabel={scope.label} sha={selectedSha} subject={selectedCommit?.subject ?? null} />
    ) : null

  return (
    <>
      <section className="panel" aria-labelledby="version-title">
        <h2 id="version-title">版本管理（已匯入的版本與 commit 圖）</h2>

        {/* 寬螢幕：左＝已匯入的版本；右＝分支下拉、選取的 commit 操作列、commit 圖（操作列在圖的上方，捲動時固定在視窗頂端）。
            窄螢幕：上下排列（已匯入的版本在最上面，不必捲動就能移除）。 */}
        <div className="version-layout">
          <ImportedVersions
            list={imported}
            refreshKey={snapshotKey}
            selectedSha={selectedSha}
            refsBySha={refBadges}
            subjectBySha={subjectBySha}
            focusOnMount={focusImported}
            onPick={pickFromList}
            onChanged={onSnapshotsChanged}
            onReload={onSnapshotsChanged}
          />

          <div className="version-main">
            <div className="toolbar">
              {refsData !== null && scope !== null && !empty && (
                <BranchSelect refs={refsData} value={scope.value} onChange={chooseScope} />
              )}
              <button type="button" className="secondary" onClick={() => setLocalTick((n) => n + 1)}>
                重新整理
              </button>
            </div>

            {refs.loading && (
              <p role="status" aria-busy="true">
                載入分支與 commit…
              </p>
            )}
            <ErrorBox error={refs.error} />
            {refsData?.truncated && (
              <p className="warning" role="alert">
                分支或標籤數量超過上限，下拉選單只列出前 500 個。
              </p>
            )}
            {empty && (
              <p className="empty">
                這個 repo 還沒有任何 commit，所以沒有版本可以瀏覽。建立第一個 commit（或在 GitHub 來源更新）後，按「重新整理」。
              </p>
            )}

            {/* 這個 repo 的背景工作提示（一行）：其他版本的進行中匯入、最近的失敗＋重試／忽略；選取 commit 自己的進度在下方匯入區，不重複 */}
            <RepoJobNotice
              repoName={repoTitle(repo)}
              jobs={repoJobs}
              snapshots={completedSnapshots}
              workerOffline={jobs.workerOffline}
              controls={jobs}
              hideActive={(job) => selectedKey !== null && jobCommitSha(job) === selectedKey}
              hideFailedId={version.job?.id ?? null}
            />

            {selectedSha !== null && (
              <section className="selected-bar" aria-label="選取的 commit 與操作">
                <CommitDetail key={`detail-${selectedSha}`} sha={selectedSha} commit={selectedCommit} />
                <VersionLoadStatus
                  key={`import-${selectedSha}`}
                  load={version}
                  imported={isImported}
                  snapshot={snapshot}
                  externalJob={externalJob}
                  onCancelResult={(job) => {
                    version.applyJob(job)
                    jobs.reload() // 共用輪詢也立刻更新（頂端的「進行中」數量、其他提示）
                  }}
                  focusImportButton={removedNotice?.sha === selectedSha}
                >
                  <VersionActions
                    repoId={repo.id}
                    sha={selectedSha}
                    snapshot={listSnapshot}
                    listLoading={importedLoading}
                    listError={imported.error}
                    onReloadList={onSnapshotsChanged}
                    onChanged={onSnapshotsChanged}
                    onRemoved={handleRemoved}
                  />
                </VersionLoadStatus>
                {removedNotice !== null && removedNotice.sha === selectedSha && !isImported && (
                  <p className="success" role="status">
                    {removedNotice.text}
                  </p>
                )}
              </section>
            )}

            <section className="version-graph" aria-labelledby="graph-title">
              <h3 id="graph-title">commit 圖</h3>
              {commits.loading && commits.items === null && (
                <p role="status" aria-busy="true">
                  載入 commit 圖…
                </p>
              )}
              <ErrorBox error={commits.error} />
              {commits.items !== null && commits.items.length === 0 && <p className="empty">這個範圍沒有任何 commit。</p>}
              {commits.items !== null && commits.items.length > 0 && (
                <>
                  <CommitGraph
                    commits={commits.items}
                    selectedSha={selectedSha}
                    loadedShas={version.loadedShas}
                    loadingShas={loadingShas}
                    defaultBranch={defaultBranch}
                    onSelect={setPicked}
                    reveal={reveal}
                  />
                  <div className="graph-footer">
                    <span className="note" role="status">
                      已顯示 {commits.items.length.toLocaleString()} 個 commit
                      {commits.hasMore ? '，還有更早的 commit' : '（已是全部）'}
                    </span>
                    {commits.hasMore && (
                      <button type="button" disabled={commits.loadingMore} onClick={commits.loadMore}>
                        {commits.loadingMore ? '載入中…' : '載入更多'}
                      </button>
                    )}
                  </div>
                  <ErrorBox error={commits.moreError} />
                </>
              )}
            </section>
          </div>
        </div>

        {/* 對應 API 放在最下方：不把操作區往下推（不必捲動就能操作是這個版面的目標） */}
        <ApiHint
          apis={[
            `GET /api/v1/repositories/${repo.id}/refs`,
            `GET /api/v1/repositories/${repo.id}/commits（每頁 100 筆；含 imported 標記）`,
            `GET /api/v1/repositories/${repo.id}/snapshots（已匯入的版本、快照編號、大小、status、is_referenced、is_stale_profile）`,
            'GET /api/v1/snapshots/{id}（選取版本的快照詳情）',
            'GET /api/v1/jobs/status（決定用背景工作，或 worker 不可用時回退同步匯入；與頁面頂端共用同一個輪詢）',
            'GET /api/v1/jobs?status=active、GET /api/v1/jobs?status=failed（同上的共用輪詢：此 repo 進行中的匯入與最近失敗的提示；重新整理頁面後仍看得到）',
            ...loadApis,
            'POST /api/v1/jobs/{id}/retry（失敗提示的「重試」）',
            'POST /api/v1/jobs/{id}/cancel（提示上的「取消」，其他版本的進行中匯入；先確認）',
            `PUT /api/v1/repositories/${repo.id}/reference（設為參考版本；body {"snapshot_id"}）`,
            `DELETE /api/v1/repositories/${repo.id}/reference（取消參考）`,
            'DELETE /api/v1/snapshots/{id}?force=（移除版本，含批次：逐一呼叫；參考版本需 force=true）',
          ]}
        />
      </section>

      {/* 檔案區預設收合：展開才掛載 FilePanel（才會逐頁載入檔案清單）。只有已匯入的版本才有檔案可看。 */}
      {selectedSha !== null && (
        <details
          className="panel advanced"
          open={filesOpen}
          onToggle={(e) => setFilesOpen(e.currentTarget.open)}
        >
          <summary>檔案（展開才會載入檔案樹；需先匯入此版本）</summary>
          {filesOpen &&
            (snapshot !== null ? (
              <FilePanel key={snapshot.id} snapshotId={snapshot.id} summary={summary} />
            ) : (
              <div className="advanced-body">
                {summary}
                {version.status === 'loading' ? (
                  <p className="note">此版本匯入完成後，檔案樹會顯示在這裡（進度見上方「選取的 commit」操作列）。</p>
                ) : isImported ? (
                  <p className="note">正在取得此版本的快照編號…</p>
                ) : (
                  <p className="empty">請先匯入此版本才能瀏覽檔案。</p>
                )}
              </div>
            ))}
        </details>
      )}
    </>
  )
}
