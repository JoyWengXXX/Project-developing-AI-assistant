import { useEffect, useRef, useState } from 'react'
import { api, type ApiError } from '../api'
import { REMOVE_ERROR_HINT, repoLabel, toApiError } from '../format'
import type { ReferenceItem, Repository, RepositoryRemoved } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'
import type { RepoImports } from '../useRepoImports'
import { clearReference, useReferences } from '../useReferences'
import type { RepoListState } from '../useRepoList'
import { ReferenceSummary } from './ReferenceSummary'
import { RepoCard } from './RepoCard'
import type { JobsOverview } from '../useJobsOverview'

interface Props {
  list: RepoListState
  /** 匯入的進度與動作（App 持有）。 */
  imports: RepoImports
  /** 頁面共用的背景工作輪詢（App 持有）；依 repo 分給各張卡片，卡片不各自發請求。 */
  jobs: JobsOverview
  /** 點卡片或「查看詳情」：進入該 repo 的詳情；「管理已匯入版本」同樣進入詳情，並把焦點移到「已匯入的版本」。 */
  onOpen: (repo: Repository, options?: { focusImported?: boolean }) => void
  /** repo 已從後端消失（移除成功或回 404）後通知 App，讓它清除對該 repo 的選取。 */
  onRemoved: (repoId: number) => void
  /** GitHub 來源 fetch 成功後通知 App（詳情的版本區據此重新整理）。 */
  onFetched: (repoId: number) => void
  /** 從詳情回來時，把焦點移到標題（鍵盤與讀屏使用者不會迷路）。 */
  focusTitle: boolean
}

// 移除的結果顯示在總覽頁上方（成功後卡片已經不在列表裡）
type RemoveNotice =
  | { kind: 'success'; text: string }
  | { kind: 'error'; label: string; error: ApiError }

/**
 * 倉庫總覽：參考版本摘要＋repo 卡片網格。
 *
 * 這一頁**不會自動匯入**任何版本：未匯入的卡片顯示「匯入」按鈕；只有註冊成功的那一次由 App 自動提交匯入。
 * 進入詳情（總覽卸載）時卡片的概況請求會取消；匯入由 App 持有，不會因為換頁中斷，回到總覽時卡片接著顯示結果。
 * 參考版本清單（GET /references）在這裡載入，設定、取消、匯入完成、移除 repo 之後重新讀取；
 * 在詳情頁移除版本後回到總覽，這一頁會重新掛載並讀取最新資料。
 */
export function RepoOverviewGrid({ list, imports, jobs, onOpen, onRemoved, onFetched, focusTitle }: Props) {
  const references = useReferences()
  const clearAction = useAction()
  const titleRef = useRef<HTMLHeadingElement>(null)

  // 移除：狀態放在總覽頁（不放在卡片），列表重新載入造成卡片重建時，「移除中」狀態不會遺失
  const [removingId, setRemovingId] = useState<number | null>(null)
  const [removeNotice, setRemoveNotice] = useState<RemoveNotice | null>(null)

  useEffect(() => {
    if (focusTitle) titleRef.current?.focus()
  }, [focusTitle])

  // 任何 repo 匯入完成（可能含「改用最新版本」的自動設為參考）：重新讀取參考清單
  const { reload: reloadReferences } = references
  const finishedImports = Object.values(imports.doneTicks).reduce((sum, n) => sum + n, 0)
  useEffect(() => {
    if (finishedImports > 0) reloadReferences()
  }, [finishedImports, reloadReferences])

  // 移除是同步請求：GitHub 來源要刪 clone 目錄，可能需要數秒；期間以 removingId 避免重複送出
  async function removeRepo(repo: Repository) {
    if (removingId !== null) return
    setRemovingId(repo.id)
    setRemoveNotice(null)
    const label = repoLabel(repo)
    try {
      const res = await api<RepositoryRemoved>('DELETE', `/api/v1/repositories/${repo.id}`)
      const r = res.data
      setRemoveNotice({
        kind: 'success',
        text: `${label} 已移除，刪除 ${r.deleted_snapshots} 個快照、${r.deleted_files} 個檔案${r.removed_clone ? '，並清除本機 clone' : ''}。`,
      })
      handleGone(repo.id)
    } catch (err) {
      const apiError = toApiError(err)
      setRemoveNotice({ kind: 'error', label, error: apiError })
      // 404：repo 早已不在後端（例如被別的視窗移除），列表與選取狀態要同步
      if (apiError.status === 404 && apiError.code === 'repository_not_found') handleGone(repo.id)
    } finally {
      setRemovingId(null)
    }
  }

  // repo 已不存在：從畫面拿掉、重新載入列表與參考清單，並通知 App 清除選取
  function handleGone(repoId: number) {
    list.dropLocal(repoId)
    list.reload()
    reloadReferences()
    onRemoved(repoId)
  }

  // 卡片的概況回 404：同一個 repo 只處理一次（避免後端列表與概況不一致時反覆重新載入）
  const reportedMissing = useRef(new Set<number>())
  function handleMissing(repoId: number) {
    if (reportedMissing.current.has(repoId)) return
    reportedMissing.current.add(repoId)
    handleGone(repoId)
  }

  // 摘要上的「取消」：DELETE reference（冪等），成功或失敗都重新讀取
  async function clearFromSummary(item: ReferenceItem) {
    await clearAction.run(async () => {
      await clearReference(item.repository.id)
    })
    reloadReferences()
  }

  const items = list.items

  return (
    <section aria-labelledby="overview-title" className="overview">
      <div className="panel">
        <div className="overview-head">
          <h2 id="overview-title" tabIndex={-1} ref={titleRef}>
            倉庫總覽
          </h2>
          <button type="button" className="secondary" disabled={list.loading} onClick={list.reload}>
            {list.loading ? '載入中…' : '重新載入列表'}
          </button>
        </div>
        <p className="note">
          流程：註冊 repo（成功後自動匯入一次預設分支最新版本）→ 在詳情的 commit 圖點選版本、按「匯入此版本」→ 回到這裡，
          在 repo 卡片把已匯入的版本設為「參考版本」。這一頁不會自動匯入；有較新版本時請自己決定要不要改用。
        </p>
        <ApiHint
          apis={[
            'GET /api/v1/repositories（列表）',
            'GET /api/v1/repositories/{id}/overview（每張卡片的概況，唯讀）',
            'GET /api/v1/repositories/{id}/snapshots?status=completed（卡片的「已匯入版本」選單）',
            'GET /api/v1/references（參考版本清單）',
            'PUT /api/v1/repositories/{id}/reference（設為參考；body {"snapshot_id"}）',
            'DELETE /api/v1/repositories/{id}/reference（取消參考）',
            'POST /api/v1/repositories/{id}/import-jobs（按「匯入」、註冊後的一次性匯入與取消後的「重新匯入」；背景工作，取消的工作以同一個 job id 重新排入）',
            'GET /api/v1/jobs/status、GET /api/v1/jobs/{id}（決定用背景工作，並輪詢進度）',
            'GET /api/v1/jobs?status=active、GET /api/v1/jobs?status=failed（頁面共用的輪詢：卡片上「背景工作進行中」與「上次背景匯入失敗」，不是每張卡片各發一次）',
            'POST /api/v1/jobs/{id}/retry（卡片上失敗提示的「重試」）',
            'POST /api/v1/jobs/{id}/cancel（匯入進行中的「取消」，先確認；同步回退的匯入無法取消）',
            'POST /api/v1/repositories/{id}/imports（deprecated：worker 不可用時的同步回退）',
            'POST /api/v1/repositories/{id}/fetch（GitHub 更新）',
            'DELETE /api/v1/repositories/{id}（移除）',
          ]}
        />
        <ReferenceSummary
          items={references.items}
          loading={references.loading}
          error={references.error}
          busy={clearAction.busy}
          actionError={clearAction.error}
          onClear={(item) => void clearFromSummary(item)}
        />
        {removeNotice?.kind === 'success' && (
          <p className="success" role="status">
            {removeNotice.text}
          </p>
        )}
        {removeNotice?.kind === 'error' && (
          <div>
            <p className="note">移除 {removeNotice.label} 失敗：</p>
            <ErrorBox error={removeNotice.error} hints={REMOVE_ERROR_HINT} />
          </div>
        )}
        <ErrorBox error={list.error} />
      </div>

      {items === null && list.loading && <p role="status">載入中…</p>}
      {items !== null && items.length === 0 && (
        <p className="panel empty">尚未註冊任何 repo。展開上方「新增 repo」，從 GitHub 或本機路徑註冊。</p>
      )}
      {items !== null && items.length > 0 && (
        <ul className="overview-grid">
          {items.map((r) => (
            <RepoCard
              key={r.id}
              repo={r}
              imports={imports}
              jobs={jobs}
              reference={references.byRepo.get(r.id) ?? null}
              onReferenceChanged={reloadReferences}
              onOpen={onOpen}
              removing={r.id === removingId}
              removeLocked={removingId !== null}
              onRemove={removeRepo}
              onFetched={onFetched}
              onMissing={handleMissing}
            />
          ))}
        </ul>
      )}
    </section>
  )
}
