import { useRef, useState } from 'react'
import { jobsSummaryText, type SummaryInput } from '../jobsOverview'
import { queueWarning } from '../jobFormat'
import type { Repository } from '../types'
import type { JobsOverview } from '../useJobsOverview'
import { JobsDialog } from './JobsDialog'

interface Props {
  jobs: JobsOverview
  /** 給完整列表把 repository_id 對應成名稱。 */
  repos: Repository[] | null
}

/** 把共用輪詢的狀態整理成摘要文字需要的輸入（取不到狀態與還在檢查要分開）。 */
function summaryInput(jobs: JobsOverview): SummaryInput {
  const { status, inProgress, openFailures } = jobs
  if (status.data === null || inProgress === null) return status.error !== null ? { kind: 'unavailable' } : { kind: 'checking' }
  // 失敗列表還沒回來（也沒出錯）：先顯示「檢查中」，避免先閃一下「背景工作：無」再變成「失敗 N」
  if (openFailures === null && jobs.failedPoll.error === null) return { kind: 'checking' }
  return { kind: 'ready', inProgress, failed: openFailures, workerOnline: status.data.worker.status === 'online' }
}

/**
 * 頁面頂端的一行背景工作摘要（平常唯一看得到的背景工作介面）：
 *   「背景工作：進行中 N · 失敗 N · 背景執行器在線／離線」，沒有進行中與失敗時是「背景工作：無 · 背景執行器在線」。
 * 失敗＝尚未忽略的失敗工作數（紅色徽章）。完整列表要按「查看所有背景工作」才會開啟（JobsDialog），關閉後焦點回到這個按鈕。
 * 畫面上的徽章對螢幕閱讀器隱藏，改由一段同義的隱藏文字（role="status"）報讀：數字真的變了才會朗讀，進度更新不會。
 */
export function JobsSummaryBar({ jobs, repos }: Props) {
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const { status, inProgress, openFailures } = jobs
  const input = summaryInput(jobs)
  const worker = status.data?.worker
  const warning = status.data !== null ? queueWarning(status.data) : null
  const failedCount = openFailures?.count ?? 0

  return (
    <section
      className="jobs-bar"
      aria-label="背景工作摘要"
      title="資料來源：GET /api/v1/jobs/status、GET /api/v1/jobs?status=active、GET /api/v1/jobs?status=failed（與 repo 卡片、詳情共用同一份輪詢）"
    >
      <span className="sr-only" role="status">
        {jobsSummaryText(input)}
      </span>
      <span className="jobs-bar-items" aria-hidden="true">
        {input.kind === 'checking' && <span className="muted">背景工作：檢查中…</span>}
        {input.kind === 'unavailable' && <span className="badge badge-failed">無法取得工作狀態</span>}
        {input.kind === 'ready' && (
          <>
            {inProgress === 0 && openFailures !== null && failedCount === 0 ? (
              <span>背景工作：無</span>
            ) : (
              <>
                <span>背景工作：</span>
                <span title={`執行中 ${status.data?.running ?? 0} · 排隊 ${status.data?.queued ?? 0}`}>
                  進行中{' '}
                  <span className={`badge ${inProgress !== null && inProgress > 0 ? 'badge-importing' : 'badge-neutral'}`}>
                    {inProgress}
                  </span>
                </span>
                <span
                  title="失敗＝尚未忽略的失敗工作數。已在 repo 卡片或完整列表按「忽略」的不計入；超過 200 筆時以「N+」表示可能偏高。"
                >
                  失敗{' '}
                  {openFailures === null ? (
                    <span className="muted">?</span>
                  ) : (
                    <span className={`badge ${failedCount > 0 ? 'badge-failed' : 'badge-neutral'}`}>
                      {failedCount > 0 && <span aria-hidden="true">✖ </span>}
                      {failedCount}
                      {openFailures.approximate ? '+' : ''}
                    </span>
                  )}
                </span>
              </>
            )}
            <span>
              背景執行器：
              {worker?.status === 'online' ? (
                <span className="badge badge-completed">在線</span>
              ) : (
                <span className="badge badge-importing" title={worker?.status === 'stale' ? '心跳已逾時' : '尚未啟動'}>
                  <span aria-hidden="true">⚠ </span>離線
                </span>
              )}
            </span>
            {warning !== null && <span className="badge badge-importing">有工作排隊，但服務未在線</span>}
          </>
        )}
      </span>
      <button
        type="button"
        className="linklike"
        ref={triggerRef}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        查看所有背景工作
      </button>
      {open && (
        <JobsDialog
          jobs={jobs}
          repos={repos}
          onClose={() => {
            setOpen(false)
            triggerRef.current?.focus() // 焦點回到開啟對話框的按鈕
          }}
        />
      )}
    </section>
  )
}
