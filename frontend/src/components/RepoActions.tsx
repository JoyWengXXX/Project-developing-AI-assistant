import { useEffect, useRef, useState } from 'react'
import { type ApiError } from '../api'
import { repoLabel, repoTitle, toApiError } from '../format'
import type { FetchSubmit } from '../githubActions'
import type { RepoJobs } from '../jobsOverview'
import type { FetchResult, Repository } from '../types'
import { ErrorBox } from '../ui'
import type { JobControls } from '../useJobsOverview'
import { FetchSummary } from './FetchSummary'
import { RepoJobNotice } from './RepoJobNotice'

interface Props {
  repo: Repository
  /** 這一筆正在移除（DELETE 進行中，由總覽頁保存狀態）。 */
  removing: boolean
  /** 有任何一筆正在移除：避免同時送出多個移除。 */
  removeLocked: boolean
  /** 使用者在確認區塊按下「確定移除」；實際的 DELETE 與結果顯示由總覽頁負責。 */
  onRemove: (repo: Repository) => void
  /** 這個 repo 有任何背景工作（匯入、更新）排隊或執行中：不能移除（後端會回 409 repository_busy，這裡先擋）。 */
  jobActive: boolean
  /** 這個 repo 的「更新（github_fetch）」工作：進行中顯示進度與取消，失敗顯示原因與重試、忽略（共用輪詢的資料）。 */
  fetchJobs: RepoJobs
  /** 最近一次更新完成的 refs 變更清單（App 持有）；沒有就是 null。 */
  fetchResult: FetchResult | null
  /** 送出更新：背景執行器在線送背景工作，否則回退同步端點（onFallback 會在回退時先被呼叫）。失敗會拋出 ApiError。 */
  onFetch: (repoId: number, onFallback: (reason: string) => void) => Promise<FetchSubmit>
  workerOffline: boolean
  controls: JobControls
}

/**
 * 卡片上的操作區：GitHub 來源專屬的「更新（fetch）」＋「移除」（需先確認）。
 * 這裡的按鈕都在卡片「可點擊主體」之外，點擊不會同時觸發「進入詳情」。
 *
 * 更新是背景工作：按下後立刻回應，進度（階段、已耗時）與取消顯示在按鈕下方（RepoJobNotice，資料來自共用輪詢，
 * 所以重新整理頁面後仍看得到）；進行中「更新」與「移除」都停用。完成後的結果清單由 App 保存（fetchResult）。
 * 背景執行器不在線時回退到已 deprecated 的同步端點：請求會一直等到 fetch 完成，這時只能顯示「更新中…」。
 */
export function RepoActions({
  repo,
  removing,
  removeLocked,
  onRemove,
  jobActive,
  fetchJobs,
  fetchResult,
  onFetch,
  workerOffline,
  controls,
}: Props) {
  const [submitting, setSubmitting] = useState(false) // 送出中（背景工作：數百毫秒；同步回退：直到 fetch 完成）
  const [syncReason, setSyncReason] = useState<string | null>(null) // 回退同步的原因（只有同步期間顯示）
  const [fetchError, setFetchError] = useState<ApiError | null>(null)
  const [confirming, setConfirming] = useState(false) // 是否顯示「確定移除？」區塊
  const removeButtonRef = useRef<HTMLButtonElement>(null)
  const cancelButtonRef = useRef<HTMLButtonElement>(null)
  const wasConfirming = useRef(false)
  const sending = useRef(false) // 同步擋住連按（state 要等下一次 render 才看得到）

  // 鍵盤焦點：打開確認區塊時移到「取消」（預設走安全的選項）；取消後回到「移除」按鈕
  useEffect(() => {
    if (confirming) cancelButtonRef.current?.focus()
    else if (wasConfirming.current) removeButtonRef.current?.focus()
    wasConfirming.current = confirming
  }, [confirming])

  async function fetchRefs() {
    if (sending.current) return
    sending.current = true
    setSubmitting(true)
    setSyncReason(null)
    setFetchError(null)
    try {
      await onFetch(repo.id, (reason) => setSyncReason(reason))
    } catch (err) {
      setFetchError(toApiError(err))
    } finally {
      sending.current = false
      setSubmitting(false)
      setSyncReason(null)
    }
  }

  const isGitHub = repo.source_type === 'github'
  const fetchActive = fetchJobs.active.length > 0
  const removeBlocked = jobActive || fetchActive || submitting
  const busyNoteId = `busy-note-${repo.id}`

  return (
    <div className="repo-actions">
      <div className="action-row">
        {isGitHub && (
          <button type="button" className="secondary" disabled={submitting || fetchActive || removing} onClick={() => void fetchRefs()}>
            {submitting ? '送出中…' : fetchActive ? '更新中…' : '更新（fetch）'}
          </button>
        )}
        <button
          type="button"
          className="danger"
          ref={removeButtonRef}
          aria-expanded={confirming}
          aria-describedby={removeBlocked && !removing ? busyNoteId : undefined}
          disabled={removeBlocked || removing || removeLocked}
          onClick={() => setConfirming(true)}
        >
          {removing && !confirming ? '移除中…' : '移除'}
        </button>
      </div>
      {removeBlocked && !removing && (
        <p className="note" id={busyNoteId}>
          有背景工作進行中，完成或取消後才能移除。
        </p>
      )}

      {confirming && (
        <div
          className="remove-confirm"
          role="group"
          aria-labelledby={`remove-title-${repo.id}`}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && !removing) setConfirming(false)
          }}
        >
          <p id={`remove-title-${repo.id}`}>
            <strong>確定要移除 {repoLabel(repo)} 嗎？</strong>
          </p>
          <p>
            {isGitHub
              ? '會刪除本機 clone 與所有快照，不影響 GitHub 上的 repo。'
              : '只會移除 RepoPilot 內的紀錄與快照，不會刪除你磁碟上的資料夾。'}
          </p>
          <div className="action-row">
            <button
              type="button"
              className="danger-solid"
              disabled={removing || removeLocked || removeBlocked}
              onClick={() => onRemove(repo)}
            >
              {removing ? '移除中…' : '確定移除'}
            </button>
            <button
              type="button"
              className="secondary"
              ref={cancelButtonRef}
              disabled={removing}
              onClick={() => setConfirming(false)}
            >
              取消
            </button>
          </div>
          {removing && (
            <p role="status" aria-busy="true">
              {isGitHub ? '正在刪除本機 clone，可能需要數秒；' : ''}這是同步請求，請等待後端回應。
            </p>
          )}
        </div>
      )}

      {isGitHub && submitting && syncReason !== null && (
        <p role="status" aria-busy="true">
          {syncReason}（同步端點已 deprecated）：fetch 中…請求會一直等到後端完成，請不要重複送出。
        </p>
      )}
      {/* 更新工作：進行中的階段與已耗時、取消；失敗的原因與重試、忽略。沒有東西可顯示時不佔空間 */}
      {isGitHub && (
        <RepoJobNotice
          repoName={repoTitle(repo)}
          jobs={fetchJobs}
          snapshots={[]}
          workerOffline={workerOffline}
          controls={controls}
        />
      )}
      {isGitHub && fetchResult !== null && !fetchActive && <FetchSummary result={fetchResult} repo={repo} />}
      <ErrorBox error={fetchError} />
    </div>
  )
}
