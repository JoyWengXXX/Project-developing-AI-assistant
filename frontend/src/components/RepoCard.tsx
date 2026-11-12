import { useEffect, useRef, useState, type MouseEvent } from 'react'
import { commitShortSha, formatBytes, formatCommitTime, repoTitle, shortRefName } from '../format'
import type { ReferenceItem, Repository, RepositoryOverview } from '../types'
import { ErrorBox } from '../ui'
import { useOnFalling, type JobsOverview } from '../useJobsOverview'
import { useOverview, type OverviewState } from '../useOverview'
import type { RepoImports } from '../useRepoImports'
import { useSnapshotList } from '../useSnapshotList'
import { ImportRunStatus } from './ImportRunStatus'
import { LanguageBar } from './LanguageBar'
import { ProjectInfoBadges } from './ProjectInfoBadges'
import { ReadmePreview } from './ReadmePreview'
import { RepoActions } from './RepoActions'
import { RepoJobNotice } from './RepoJobNotice'
import { RepoReference } from './RepoReference'

interface Props {
  repo: Repository
  /** 匯入的進度與動作（App 持有，註冊後的一次性匯入也在這裡）。 */
  imports: RepoImports
  /** 頁面共用的背景工作輪詢（App 持有）：卡片只取自己這個 repo 的進行中與失敗工作，不另外發請求。 */
  jobs: JobsOverview
  /** 這個 repo 的參考版本（GET /references）；沒有為 null。 */
  reference: ReferenceItem | null
  /** 設定／取消參考、或重新整理後：通知總覽頁重新讀取參考清單。 */
  onReferenceChanged: () => void
  onOpen: (repo: Repository, options?: { focusImported?: boolean }) => void
  removing: boolean
  removeLocked: boolean
  onRemove: (repo: Repository) => void
  onFetched: (repoId: number) => void
  /** 概況回 404（repo 已不存在）：通知總覽頁同步列表。 */
  onMissing: (repoId: number) => void
}

/** 預設分支的顯示：優先用概況的 default_ref，其次是註冊時記錄的 GitHub 預設分支。 */
function branchText(repo: Repository, overview: RepositoryOverview | null): string | null {
  if (overview?.default_ref) return shortRefName(overview.default_ref)
  if (repo.github_default_branch) return repo.github_default_branch
  if (overview?.head && repo.source_type === 'local') return 'HEAD（沒有指向分支）'
  return null
}

/**
 * 一張 repo 卡片：標題、來源、最新 commit、概況（README／語言／統計／專案資訊）、參考版本與操作。
 * 卡片**不會自動匯入**：預設分支最新版本還沒匯入時，顯示「尚未匯入」與「匯入」按鈕，由使用者決定。
 */
export function RepoCard(props: Props) {
  const { repo, imports, jobs, reference, onReferenceChanged, onOpen, removing, removeLocked } = props
  const run = imports.runs[repo.id]
  const doneTicks = imports.doneTicks[repo.id] ?? 0
  const state = useOverview(repo.id, doneTicks)
  const [listTick, setListTick] = useState(0)
  const snapshots = useSnapshotList(repo.id, 'completed', doneTicks + listTick)
  const { overview } = state
  const isGitHub = repo.source_type === 'github'
  const title = repoTitle(repo)
  const branch = branchText(repo, overview)
  const missing = state.loadError?.code === 'repository_not_found'
  const repoJobs = jobs.forRepo(repo.id)
  const jobActive = repoJobs.active.length > 0

  // 這個 repo 的背景工作全部結束（例如重新整理頁面前就開始的匯入，或在這裡按「重試」）：重新讀取概況與已匯入版本。
  // 這次操作自己的匯入（run）結束時，已由 doneTicks 重新讀取，不重複
  useOnFalling(jobActive && run === undefined, refresh)

  // 匯入完成後概況重新讀取完畢：結果已經顯示在卡片上，清掉這次的紀錄；
  // 若概況仍說「尚未匯入」（例如匯入設定不一致），改成失敗狀態並提醒，避免使用者以為沒反應、也不會自動重送
  const { settle } = imports
  const runStatus = run?.status
  useEffect(() => {
    if (runStatus === 'done' && !state.refreshing) settle(repo.id, state.loadError !== null || state.phase !== 'unanalyzed')
  }, [runStatus, state.refreshing, state.phase, state.loadError, settle, repo.id])

  // repo 已不存在時只通知一次（通知會讓列表重新載入，之後這張卡片就會被卸載）
  const { onMissing } = props
  const notifiedMissing = useRef(false)
  useEffect(() => {
    if (missing && !notifiedMissing.current) {
      notifiedMissing.current = true
      onMissing(repo.id)
    }
  }, [missing, onMissing, repo.id])

  // 點卡片主體進入詳情；按鈕、連結、輸入控制項、展開鈕與「正在選取文字」不算
  function handleBodyClick(e: MouseEvent<HTMLDivElement>) {
    if ((e.target as HTMLElement).closest('button, a, input, select, label, summary, .repo-job-notice')) return
    if ((window.getSelection()?.toString() ?? '') !== '') return
    if (!removing && !removeLocked) onOpen(repo)
  }

  function startImport(options?: { thenReference?: boolean }) {
    if (overview?.head) imports.start(repo.id, overview.head.sha, options)
  }

  function refresh() {
    state.reload()
    setListTick((n) => n + 1)
    onReferenceChanged()
  }

  const showReference = state.phase === 'ready' || state.phase === 'unanalyzed'

  return (
    <li className="repo-card panel" aria-label={title}>
      <div className="card-body" onClick={handleBodyClick}>
        <div className="card-head">
          <h3 className="card-title" title={repo.canonical_local_path}>
            {title}
          </h3>
          <span className="badges">
            <span className="badge badge-neutral">{isGitHub ? 'GitHub' : '本機'}</span>
            {isGitHub && repo.github_is_private === true && <span className="badge badge-importing">私有</span>}
            {isGitHub && repo.github_is_private === false && <span className="badge badge-completed">公開</span>}
          </span>
        </div>
        <div className="muted card-meta">
          #{repo.id} · 預設分支：{branch !== null ? <code>{branch}</code> : '—'}
        </div>

        {overview?.head && (
          <div className="card-commit" title={`${overview.head.sha}\n${overview.head.subject}`}>
            <code>{commitShortSha(overview.head.sha)}</code>
            <span className="commit-subject">{overview.head.subject || '（沒有訊息）'}</span>
            <span className="muted commit-time">{formatCommitTime(overview.head.authored_at)}</span>
          </div>
        )}

        {/* 這個 repo 的背景工作：進行中（含重新整理頁面前就開始的）與最近的失敗＋重試／忽略。這次操作自己的進度（ImportRunStatus）不重複顯示 */}
        <RepoJobNotice
          repoName={title}
          jobs={repoJobs}
          snapshots={snapshots.items}
          workerOffline={jobs.workerOffline}
          controls={jobs}
          hideActive={(job) => run?.status === 'running' && (run.job === null || run.job.id === job.id)}
          hideFailedId={run !== undefined && run.status !== 'done' ? (run.job?.id ?? null) : null}
        />

        <PhaseContent
          state={state}
          overview={overview}
          imports={imports}
          repoId={repo.id}
          jobActive={jobActive}
          onImport={() => startImport()}
        />
      </div>

      <div className="card-footer">
        {showReference && (
          <RepoReference
            repoId={repo.id}
            title={title}
            overview={overview}
            defaultBranch={overview?.default_ref ? shortRefName(overview.default_ref) : repo.github_default_branch}
            reference={reference}
            snapshots={snapshots}
            importLocked={imports.busy}
            onSwitchToLatest={() => startImport({ thenReference: true })}
            onChanged={() => {
              setListTick((n) => n + 1)
              onReferenceChanged()
            }}
          />
        )}
        <div className="action-row">
          <button type="button" disabled={removing || removeLocked} onClick={() => onOpen(repo)}>
            查看詳情
          </button>
          <button type="button" className="linklike" disabled={removing || removeLocked} onClick={() => onOpen(repo, { focusImported: true })}>
            管理已匯入版本
          </button>
          <button type="button" className="secondary" disabled={state.refreshing} onClick={refresh}>
            {state.refreshing ? '載入中…' : '重新整理概況'}
          </button>
        </div>
        <RepoActions
          repo={repo}
          removing={removing}
          removeLocked={removeLocked}
          onRemove={props.onRemove}
          onFetched={(id) => {
            refresh() // head 可能改變：重新載入概況（有較新版本的提示會出現；不會自動匯入）
            props.onFetched(id)
          }}
        />
      </div>
    </li>
  )
}

/** 依階段顯示的卡片內容。 */
function PhaseContent({
  state,
  overview,
  imports,
  repoId,
  jobActive,
  onImport,
}: {
  state: OverviewState
  overview: RepositoryOverview | null
  imports: RepoImports
  repoId: number
  /** 這個 repo 已有進行中的背景工作（上方的提示正在顯示進度）：停用「匯入」，避免重複送出。 */
  jobActive: boolean
  onImport: () => void
}) {
  const { phase } = state
  const run = imports.runs[repoId]
  // 空 repo 沒有東西可匯入：註冊後自動匯入的失敗（沒有 commit）不必顯示成錯誤
  const showRun = run !== undefined && run.status !== 'done' && phase !== 'empty'
  const finishing = run?.status === 'done' // 匯入剛完成，等概況重新讀取
  return (
    <>
      {/* 概況載入失敗：已有舊資料時錯誤顯示在內容上方，沒有資料時就是這張卡片的主要內容 */}
      <ErrorBox error={state.loadError} />
      {phase === 'loading' && (
        <p role="status" aria-busy="true">
          載入概況中…
        </p>
      )}
      {phase === 'empty' && (
        <p className="empty">
          這個 repo 還沒有 commit。
          <span className="note"> （空 repo，或找不到預設分支；有 commit 後按「重新整理概況」。）</span>
        </p>
      )}
      {showRun && (
        <ImportRunStatus
          run={run}
          locked={imports.busy}
          onRetry={() => imports.start(repoId, run.revision, { thenReference: run.thenReference })}
          onDismiss={() => imports.dismiss(repoId)}
        />
      )}
      {finishing && (
        <p role="status" aria-busy="true">
          匯入完成，更新概況中…
        </p>
      )}
      {phase === 'unanalyzed' && !showRun && !finishing && (
        <div className="unanalyzed">
          <p>
            <strong>尚未匯入預設分支最新版本。</strong>
          </p>
          <p className="note">把這個版本存進資料庫，供之後搜尋與 Agent 參考。不會自動匯入，需要時請按「匯入」。</p>
          <button type="button" disabled={imports.busy || jobActive} onClick={onImport}>
            匯入
          </button>
          {imports.busy && <p className="note">另一個 repo 正在匯入，完成後再按（一次只匯入一個）。</p>}
          {jobActive && <p className="note">這個 repo 已有背景工作進行中（見上方），完成後再按。</p>}
        </div>
      )}
      {phase === 'ready' && overview !== null && <ReadyContent overview={overview} />}
    </>
  )
}

function ReadyContent({ overview }: { overview: RepositoryOverview }) {
  const stats = overview.stats
  return (
    <>
      {stats !== null && (
        <dl className="card-stats">
          <div>
            <dt>檔案</dt>
            <dd>{stats.imported_files.toLocaleString('zh-TW')}</dd>
          </div>
          <div>
            <dt>跳過</dt>
            <dd>{stats.skipped_files.toLocaleString('zh-TW')}</dd>
          </div>
          <div>
            <dt>總大小</dt>
            <dd>{formatBytes(stats.total_bytes)}</dd>
          </div>
        </dl>
      )}
      <h4>README</h4>
      <ReadmePreview readme={overview.readme} />
      <h4>語言分布</h4>
      <LanguageBar languages={overview.languages} />
      <ProjectInfoBadges items={overview.project_info} />
    </>
  )
}
