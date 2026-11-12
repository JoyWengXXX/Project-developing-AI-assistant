import { useState } from 'react'
import { ERROR_HINT, formatTime } from '../format'
import { durationBetween, formatDuration, JOB_KIND_LABEL, JOB_PHASE_LABEL } from '../jobFormat'
import type { Job } from '../types'
import { JobStatusBadge } from './JobStatusBadge'

/** params／result 是不可信內容：一律轉成文字放進 <pre>，由 React 轉義，不會被當 HTML 解析。 */
function formatJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? ''
  } catch {
    return String(value)
  }
}

function CopyRequestId({ requestId }: { requestId: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  async function copy() {
    try {
      await navigator.clipboard.writeText(requestId)
      setState('copied')
    } catch {
      setState('failed') // 非安全環境或權限被拒：請使用者手動選取
    }
  }
  return (
    <>
      <code className="wrap">{requestId}</code>{' '}
      <button type="button" className="secondary small" onClick={() => void copy()}>
        複製
      </button>{' '}
      <span role="status" className="muted">
        {state === 'copied' && '已複製'}
        {state === 'failed' && '複製失敗，請手動選取'}
      </span>
    </>
  )
}

function TimelineItem({ label, iso, note }: { label: string; iso: string | null; note?: string }) {
  if (iso === null) return null
  return (
    <li>
      <span className="timeline-label">{label}</span> {formatTime(iso)}
      {note && <span className="muted">（{note}）</span>}
    </li>
  )
}

/** 單一工作的全欄位（唯讀）：基本資料、進度計數、時間軸、params、result；request_id 供對照後端日誌。 */
export function JobDetail({ job, repoName, id }: { job: Job; repoName: string; id: string }) {
  const p = job.progress
  const waited = durationBetween(job.created_at, job.started_at)
  const ran = durationBetween(job.started_at, job.finished_at)
  return (
    <div className="job-detail" id={id}>
      <h4>工作 #{job.id} 詳情</h4>
      <dl className="kv">
        <dt>類型</dt>
        <dd>
          {JOB_KIND_LABEL[job.kind]}（<code>{job.kind}</code>）
        </dd>
        <dt>狀態</dt>
        <dd>
          <JobStatusBadge status={job.status} />
          {job.cancel_requested && <span className="muted"> 已要求取消</span>}
        </dd>
        <dt>階段</dt>
        <dd>
          {JOB_PHASE_LABEL[job.phase]}（<code>{job.phase}</code>）
        </dd>
        <dt>repo</dt>
        <dd>{repoName}</dd>
        <dt>snapshot_id</dt>
        <dd>{job.snapshot_id ?? '（無）'}</dd>
        <dt>嘗試次數</dt>
        <dd>
          {job.attempt} / {job.max_attempts}
        </dd>
        <dt>request_id</dt>
        <dd>{job.request_id ? <CopyRequestId requestId={job.request_id} /> : '（無）'}</dd>
        <dt>{job.status === 'queued' ? '上次失敗原因' : '錯誤'}</dt>
        <dd>
          {job.error ? (
            <>
              <code>{job.error.code}</code>：{job.error.message}
              {ERROR_HINT[job.error.code] && <div className="muted">說明：{ERROR_HINT[job.error.code]}</div>}
            </>
          ) : (
            '（無）'
          )}
        </dd>
      </dl>

      <h4>進度計數</h4>
      <dl className="kv">
        <dt>processed_files</dt>
        <dd>{p.processed_files}</dd>
        <dt>total_files</dt>
        <dd>{p.total_files ?? '（未知）'}</dd>
        <dt>indexed_files</dt>
        <dd>{p.indexed_files}</dd>
        <dt>parse_error_count</dt>
        <dd>{p.parse_error_count}</dd>
        <dt>bytes_received</dt>
        <dd>{p.bytes_received ?? '（未量測）'}</dd>
      </dl>

      <h4>時間軸</h4>
      <ol className="job-timeline">
        <TimelineItem label="建立" iso={job.created_at} />
        <TimelineItem
          label="開始執行"
          iso={job.started_at}
          note={waited !== null ? `排隊等待 ${formatDuration(waited)}` : undefined}
        />
        <TimelineItem label="本階段開始" iso={job.phase_started_at} />
        <TimelineItem label="最後心跳" iso={job.last_heartbeat_at} />
        <TimelineItem label="預定下次重試" iso={job.next_attempt_at} />
        <TimelineItem
          label="結束"
          iso={job.finished_at}
          note={ran !== null ? `執行 ${formatDuration(ran)}` : undefined}
        />
        <TimelineItem label="最後更新" iso={job.updated_at} />
      </ol>

      <h4>params（建立時的參數，唯讀文字）</h4>
      <pre className="job-json" tabIndex={0} aria-label={`工作 #${job.id} 的 params`}>
        {formatJson(job.params)}
      </pre>
      <h4>result（結果，唯讀文字）</h4>
      {job.result === null ? (
        <p className="muted">（尚無結果）</p>
      ) : (
        <pre className="job-json" tabIndex={0} aria-label={`工作 #${job.id} 的 result`}>
          {formatJson(job.result)}
        </pre>
      )}
    </div>
  )
}
