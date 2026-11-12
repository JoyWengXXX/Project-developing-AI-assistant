import { useEffect, useRef, type ReactNode } from 'react'
import { JOB_PHASE_LABEL, formatSize, hasFailureDetails, isCancelling, canCancel, queueWarning } from '../jobFormat'
import type { Job, Snapshot } from '../types'
import { ErrorBox } from '../ui'
import type { VersionLoad } from '../useVersionLoad'
import { useFocusIfLost } from '../useFocusIfLost'
import { methodNote } from '../versionJobs'
import { CancelJobButton } from './CancelJobButton'
import { JobFailureInfo } from './JobFailureInfo'
import { JobProgress } from './JobProgress'
import { JobStatusBadge } from './JobStatusBadge'

// 此情境專用的說明（優先於通用的 ERROR_HINT）
const LOAD_ERROR_HINT: Record<string, string> = {
  job_not_found: '這個背景工作已不存在（例如 repo 已被移除）；按「重試匯入」會重新提交。',
}

interface Props {
  load: VersionLoad
  /** 這個版本已匯入（commits 的 imported 欄位、已匯入版本列表，或本次匯入成功；由 VersionPanel 判斷）。 */
  imported: boolean
  /** 已知的快照（編號與大小）；已匯入但還沒取得時為 null。 */
  snapshot: Snapshot | null
  /**
   * 頁面共用輪詢看到、對應目前選取 commit 的進行中匯入工作（例如重新整理頁面前就開始的）。
   * 只有「沒有這次操作的狀態」時才用它補上進度；有 load.job 時以 load 為主，不會出現兩份進度。
   */
  externalJob?: Job | null
  /** 取消請求成功後（POST /jobs/{id}/cancel 回來的最新 job）：父元件把它併入 load 並重新讀取共用輪詢。 */
  onCancelResult: (job: Job) => void
  /** 剛移除了這個版本：畫面換成「匯入此版本」時，把鍵盤焦點移到那顆按鈕（原本按的「移除」已不在畫面上）。 */
  focusImportButton?: boolean
  /** 已匯入時的管理動作（設為參考、移除）。 */
  children?: ReactNode
}

/**
 * 選取的 commit 的匯入區（版本區，檔案區收合時也看得到）。**選取 commit 不會自動匯入**，五種狀態：
 *   - 未匯入：說明與「匯入此版本」按鈕（worker 不在線時說明將改用同步匯入）。
 *   - 匯入中：背景工作的狀態徽章、階段、進度（總數未知時「正在列舉檔案…」，已知才有百分比與批次）、嘗試次數、退避倒數，
 *     以及「取消匯入」（行內確認；取消中顯示「取消中…」直到工作停下來）。同步回退的匯入無法取消，只說明這點。
 *     排隊中而 worker／dispatcher 不在線時，顯示與完整列表相同的警告。
 *   - 已取消：說明與「重新匯入此版本」（同一個工作重新排入）。
 *   - 已匯入：快照摘要與管理動作（children）；檔案區預設收合，不自動展開。
 *   - 失敗：失敗追查資訊（JobFailureInfo：code、繁中說明、嘗試次數、request_id…）與「重試匯入」。
 * 最下方一行小字說明這次用的是背景工作、既有快照，或（worker 不可用時）同步回退。
 * 只有 aria-live 的那一行會被螢幕閱讀器朗讀；進度數字每 1.5 秒更新，不放進 live 區以免一直朗讀。
 */
export function VersionLoadStatus({ load, imported, snapshot, externalJob = null, onCancelResult, focusImportButton = false, children }: Props) {
  const { status, error, failedJob, method, jobsStatus, pollError, decision } = load
  // 沒有這次操作的狀態（idle、沒有 job）時，才用共用輪詢的工作資料補上進度
  const external = status === 'idle' && load.job === null ? externalJob : null
  const job = load.job ?? external
  const importButtonRef = useRef<HTMLButtonElement>(null)
  const rootRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (focusImportButton && !imported) importButtonRef.current?.focus()
  }, [focusImportButton, imported])
  // 執行中的取消要等 worker 停下來：「取消中…」變成「已取消」時，原本的按鈕消失，焦點移到這一區
  useFocusIfLost(rootRef, job !== null && job.status === 'cancelled')

  // 排隊中而 worker 不在線：不管 /jobs/status 的 queued 計數有沒有更新，這個 job 本身就在排隊
  const warning =
    job !== null && job.status === 'queued' && jobsStatus !== null
      ? queueWarning({ ...jobsStatus, queued: Math.max(jobsStatus.queued, 1) })
      : null
  const jobActive = job !== null && canCancel(job)

  // 取消成功後，原本的「取消匯入」可能已不在畫面上（立即取消、或工作早已結束）：把焦點移到這一區，鍵盤使用者不會掉到頁面最上方
  function handleCancelResult(result: Job) {
    onCancelResult(result)
    rootRef.current?.focus()
  }

  // ---- 已匯入 ----
  if (imported) {
    return (
      <section className="version-load" aria-label="此版本的匯入狀態" ref={rootRef} tabIndex={-1}>
        <p className="version-load-summary" role="status">
          <span className="badge badge-completed">
            <span aria-hidden="true">✔ </span>已匯入
          </span>{' '}
          {snapshot !== null
            ? `快照 #${snapshot.id} · ${snapshot.file_count.toLocaleString()} 個檔案 · ${formatSize(snapshot.total_bytes)}（瀏覽檔案：本頁下方「檔案」區）`
            : '瀏覽檔案：本頁下方「檔案」區'}
        </p>
        {children}
      </section>
    )
  }

  // ---- 失敗 ----
  if (status === 'error' && error !== null) {
    return (
      <section className="version-load" aria-label="此版本的匯入狀態" ref={rootRef} tabIndex={-1}>
        <p className="version-load-summary" role="status">
          匯入失敗
        </p>
        {error.code === 'snapshot_import_in_progress' && (
          <p className="warning" role="status">
            <strong>另一個匯入正在進行這個版本。</strong>稍後再按「重試匯入」即可，不是失敗。
          </p>
        )}
        {/* 背景工作失敗：與卡片提示、工作列表詳情相同的追查資訊；請求本身的錯誤（409／422…）用 ErrorBox */}
        {failedJob !== null ? <JobFailureInfo job={failedJob} /> : <ErrorBox error={error} hints={LOAD_ERROR_HINT} />}
        <button type="button" onClick={load.retry} disabled={load.decision.mode === 'wait'}>
          重試匯入
        </button>
        {method !== null && <p className="note version-load-method">{methodNote(method)}</p>}
      </section>
    )
  }

  // ---- 匯入中 ----
  if (status === 'loading' || external !== null) {
    const cancelling = job !== null && isCancelling(job)
    let summary: ReactNode
    if (job !== null && jobActive) {
      summary = (
        <>
          <JobStatusBadge status={job.status} cancelling={cancelling} />{' '}
          {cancelling ? '正在取消這次匯入（等目前這一批處理完就會停止）' : job.status === 'queued' ? '等候 worker 取用' : '正在匯入此版本'}
          （階段：{JOB_PHASE_LABEL[job.phase]}）
        </>
      )
    } else if (job !== null && job.status === 'succeeded') {
      summary = '匯入完成，正在取得快照…'
    } else if (method?.kind === 'sync') {
      summary = '正在同步匯入此版本…第一次匯入會讀取所有檔案並保存，大型版本可能需要數秒到數分鐘。'
    } else {
      summary = '正在送出匯入…'
    }
    return (
      <section className="version-load" aria-label="此版本的匯入狀態" ref={rootRef} tabIndex={-1}>
        <p className="version-load-summary" role="status" aria-busy="true">
          {summary}
        </p>
        {job !== null && jobActive && (
          <>
            <JobProgress job={job} />
            <p className="job-meta muted">
              工作 #{job.id}
              {!hasFailureDetails(job) && ` · 嘗試 ${job.attempt} / ${job.max_attempts}`}
            </p>
            <JobFailureInfo job={job} />
            {warning !== null && (
              <div className="warning" role="status">
                <strong>
                  <span aria-hidden="true">⚠ </span>
                  {warning}
                </strong>
              </div>
            )}
            <CancelJobButton job={job} onResult={handleCancelResult} />
          </>
        )}
        {pollError !== null && (
          <>
            <ErrorBox error={pollError} />
            <p className="note">畫面上的進度可能已過期，會自動重試取得（失敗時逐步拉長間隔）。</p>
          </>
        )}
        {method?.kind === 'sync' && <p className="note">同步載入無法取消。</p>}
        {method !== null && <p className="note version-load-method">{methodNote(method)}</p>}
        {external !== null && (
          <p className="note version-load-method">
            匯入方式：背景工作（這個匯入不是這次瀏覽中送出的，進度取自頁面共用的輪詢，約每 2.5 秒更新一次）。
          </p>
        )}
      </section>
    )
  }

  // ---- 已取消（重新匯入＝同一個工作重新排入，見 useVersionLoad） ----
  if (job !== null && job.status === 'cancelled') {
    return (
      <section className="version-load" aria-label="此版本的匯入狀態" ref={rootRef} tabIndex={-1}>
        <p className="version-load-summary" role="status">
          <JobStatusBadge status="cancelled" /> 已讀取的內容沒有保留，可以重新匯入。
        </p>
        <JobFailureInfo job={job} />
        <div className="action-row">
          <button type="button" ref={importButtonRef} disabled={decision.mode === 'wait'} onClick={load.start}>
            重新匯入此版本
          </button>
        </div>
        {decision.mode === 'sync' && (
          <p className="warning" role="status">
            <strong>
              <span aria-hidden="true">⚠ </span>
              {decision.reason}。
            </strong>
            同步匯入會一直占住請求直到完成，畫面不會顯示進度，也無法取消。
          </p>
        )}
      </section>
    )
  }

  // ---- 未匯入 ----
  return (
    <section className="version-load" aria-label="此版本的匯入狀態" ref={rootRef} tabIndex={-1}>
      <p className="version-load-summary">
        <span className="badge badge-neutral">尚未匯入</span> 選取 commit 只會顯示資訊，不會自動匯入。
      </p>
      <div className="action-row">
        <button type="button" ref={importButtonRef} disabled={decision.mode === 'wait'} onClick={load.start}>
          匯入此版本
        </button>
        <span className="note">把這個版本存進資料庫，供之後搜尋與 Agent 參考。</span>
      </div>
      {decision.mode === 'wait' && <p className="note">正在確認背景工作執行器（worker）的狀態…</p>}
      {decision.mode === 'sync' && (
        <p className="warning" role="status">
          <strong>
            <span aria-hidden="true">⚠ </span>
            {decision.reason}。
          </strong>
          同步匯入會一直占住請求直到完成（大型版本可能需要數秒到數分鐘），畫面不會顯示進度。
        </p>
      )}
      {decision.mode === 'job' && <p className="note">會以背景工作匯入（由 worker 執行，進度約每 1.5 秒更新一次）。</p>}
    </section>
  )
}
