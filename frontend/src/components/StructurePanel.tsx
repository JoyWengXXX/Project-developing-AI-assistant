import { useEffect, useRef, useState } from 'react'
import { api } from '../api'
import { formatTime, jobFailure } from '../format'
import { queueWarning } from '../jobFormat'
import { analyzersText, INDEX_STATUS_LABEL, kindLabel, LANGUAGE_LABEL, PARSE_STATUS_LABEL } from '../structure'
import type { IndexCounts, Job, JobsStatus, JobSubmitResponse, SnapshotIndex } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { useLoad } from '../useLoad'
import { usePolling } from '../usePolling'
import { useTrackedJob } from '../useTrackedJob'
import { isFinished } from '../versionJobs'
import { JobProgress } from './JobProgress'
import { PollErrorBox } from './PollErrorBox'
import { ProblemFiles } from './ProblemFiles'
import { SymbolDetailView } from './SymbolDetailView'
import { SymbolTree } from './SymbolTree'

const FAST_POLL_MS = 2_000
const IDLE_POLL_MS = 30_000

function CountList({ title, counts, labels }: { title: string; counts: Record<string, number>; labels?: Record<string, string> }) {
  const entries = Object.entries(counts)
  return (
    <div>
      <h4>{title}</h4>
      {entries.length === 0 ? (
        <p className="empty">（空）</p>
      ) : (
        <table className="table compact">
          <tbody>
            {entries.map(([key, n]) => (
              <tr key={key}>
                <th scope="row">{labels?.[key] ?? key}</th>
                <td>{n.toLocaleString('zh-TW')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

function Counts({ counts }: { counts: IndexCounts }) {
  const parseLabels: Record<string, string> = PARSE_STATUS_LABEL
  const kinds = Object.fromEntries(Object.keys(counts.symbols_by_kind).map((k) => [k, kindLabel(k)]))
  return (
    <>
      <div className="two-col">
        <CountList title="檔案（依語言）" counts={counts.files_by_language} labels={LANGUAGE_LABEL} />
        <CountList title="檔案（依解析狀態）" counts={counts.files_by_parse_status} labels={parseLabels} />
      </div>
      <CountList title={`符號（共 ${counts.symbols.toLocaleString('zh-TW')} 個，依種類）`} counts={counts.symbols_by_kind} labels={kinds} />
      {counts.truncated_files > 0 && (
        <p className="warning" role="alert">
          有 {counts.truncated_files} 個檔案的符號數超過單檔上限，只保留前面的符號。
        </p>
      )}
    </>
  )
}

/** 追蹤「建立索引」工作到結束；結束時通知父元件重新讀取索引狀態。以 key={job.id} 掛載。 */
function IndexJobStatus({ job: initial, onFinished }: { job: Job; onFinished: () => void }) {
  const job = useTrackedJob(initial)
  const done = isFinished(job)
  // 結束時只通知一次（onFinished 每次 render 都是新的函式，不能放進依賴，否則會不停重新載入）
  const callback = useRef(onFinished)
  const notified = useRef(false)
  useEffect(() => {
    callback.current = onFinished
  })
  useEffect(() => {
    if (done && !notified.current) {
      notified.current = true
      callback.current()
    }
  }, [done])
  return (
    <div className="detail" aria-live="polite">
      <h4>索引工作 #{job.id}</h4>
      <ApiHint apis={[`GET /api/v1/jobs/${job.id}`]} />
      {job.status === 'queued' && <p role="status">排隊中，等待背景執行器。</p>}
      {!done && <JobProgress job={job} />}
      {job.status === 'succeeded' && <p className="success">索引工作已完成。</p>}
      {(job.status === 'failed' || job.status === 'cancelled') && <ErrorBox error={jobFailure(job)} />}
    </div>
  )
}

/**
 * 「程式結構」：快照在目前設定下的結構索引（唯讀＋「建立索引」）。
 * 一律稱「部分解析」，不宣稱完整解析；Python 以 ast 做語法層級解析（不執行、不 import 受分析的程式），
 * 不含語意分析；其他語言的檔案只保存文字，不產生符號。只在使用者展開時才掛載、才發請求。
 */
export function StructurePanel({ snapshotId }: { snapshotId: number }) {
  const [reload, setReload] = useState(0)
  const [job, setJob] = useState<Job | null>(null)
  const [selected, setSelected] = useState<number | null>(null)
  const [prefixInput, setPrefixInput] = useState('')
  const [prefix, setPrefix] = useState('')
  const action = useAction()
  const tracking = job !== null && !isFinished(job)

  const path = `/api/v1/snapshots/${snapshotId}/index`
  const index = usePolling<SnapshotIndex>(path, {
    enabled: true,
    reloadKey: reload,
    intervalMs: (d) => (d?.status === 'building' || tracking ? FAST_POLL_MS : IDLE_POLL_MS),
  })
  const status = useLoad<JobsStatus>('/api/v1/jobs/status', reload)
  const warning = tracking && status.data !== null ? queueWarning(status.data) : null
  const data = index.data

  function bump() {
    setReload((n) => n + 1)
  }

  function build() {
    void action.run(async () => {
      const res = await api<JobSubmitResponse>('POST', `/api/v1/snapshots/${snapshotId}/index-jobs`)
      if (res.data.job !== null && !res.data.reused) setJob(res.data.job)
      bump()
      return res.data.reused ? '目前設定的索引已經是最新的，不需要重新建立。' : undefined
    })
  }

  const canBuild = !tracking && data?.status !== 'building'
  return (
    <div className="detail structure-panel">
      <h4>程式結構（快照 #{snapshotId}）</h4>
      <ApiHint apis={[`GET ${path}`, `POST /api/v1/snapshots/${snapshotId}/index-jobs`]} />
      <p className="note">
        Python 檔案以 ast、C# 檔案以 tree-sitter 做語法層級解析（不執行、不編譯、不載入受分析的程式），不含語意分析；
        解析結果一律稱「部分解析」，不宣稱完整。C# 的 partial 類別每個檔案各一筆（不合併）、#if 的各分支都會列出並標示「條件式定義」。
        其他語言的檔案只保存文字，不產生符號。
      </p>
      {index.loading && <p role="status">載入中…</p>}
      <PollErrorBox state={index} />
      {data && (
        <dl className="kv">
          <dt>索引狀態</dt>
          <dd>
            {INDEX_STATUS_LABEL[data.status]}
            {data.status === 'building' && '（背景工作進行中，約每 2 秒更新）'}
          </dd>
          {data.status !== 'none' && (
            <>
              <dt>抽取器</dt>
              <dd>{analyzersText(data.analyzers)}</dd>
              <dt>開始／完成</dt>
              <dd>
                {formatTime(data.started_at)} ／ {formatTime(data.completed_at)}
              </dd>
            </>
          )}
          {data.error_summary !== null && (
            <>
              <dt>失敗原因</dt>
              <dd>{data.error_summary}</dd>
            </>
          )}
        </dl>
      )}
      {data && data.status === 'none' && (
        <p className="note">這個版本在目前設定下還沒有索引。按「建立索引」會排入背景工作（只讀資料庫裡的快照內容，不會動你的 Git repo）。</p>
      )}
      {data && data.status === 'failed' && <p className="note">索引建立失敗；已發布的快照不受影響，可以重新建立。</p>}
      <div className="action-row">
        <button type="button" disabled={!canBuild || action.busy || data === null} onClick={build}>
          {data?.status === 'completed' ? '重新建立索引' : '建立索引'}
        </button>
      </div>
      {action.notice !== null && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} />
      {warning !== null && (
        <p className="warning" role="alert">
          {warning}
        </p>
      )}
      {job !== null && <IndexJobStatus key={job.id} job={job} onFinished={bump} />}

      {data && data.status === 'completed' && data.counts !== null && (
        <>
          <Counts counts={data.counts} />
          <h4>部分解析與失敗的檔案</h4>
          <ProblemFiles snapshotId={snapshotId} />
          <h4>符號樹</h4>
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault()
              setPrefix(prefixInput.trim())
            }}
          >
            <label>
              只看路徑前綴{' '}
              <input type="text" value={prefixInput} maxLength={1024} placeholder="例如 src/（區分大小寫）" onChange={(e) => setPrefixInput(e.target.value)} />
            </label>{' '}
            <button type="submit" className="secondary small">
              套用
            </button>
          </form>
          <div className="two-col symbol-columns">
            <SymbolTree key={prefix} snapshotId={snapshotId} pathPrefix={prefix} selectedId={selected} onSelect={setSelected} />
            {selected !== null ? (
              <SymbolDetailView key={selected} snapshotId={snapshotId} symbolId={selected} />
            ) : (
              <p className="empty">在左邊的符號樹點選一個符號，這裡會顯示它的位置與關係。</p>
            )}
          </div>
        </>
      )}
    </div>
  )
}
