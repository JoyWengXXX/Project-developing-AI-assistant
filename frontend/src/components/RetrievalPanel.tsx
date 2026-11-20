import { useEffect, useRef, useState } from 'react'
import { api, isAbortError, type ApiError } from '../api'
import { jobFailure, repoLabel, toApiError } from '../format'
import { JOB_STATUS_LABEL } from '../jobFormat'
import type {
  EmbeddingDevice, EmbeddingDevices, JobSubmitResponse, Repository, RetrievalIndex, RetrievalSearchResponse, SnapshotItem,
} from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { usePolling } from '../usePolling'
import { useSnapshotList } from '../useSnapshotList'
import { isFinished } from '../versionJobs'
import { JobProgress } from './JobProgress'
import { PollErrorBox } from './PollErrorBox'
import { DEFAULT_SCOPE, scopeBody, type SearchScope } from '../searchScope'
import { SearchScopePicker } from './SearchScopePicker'

type Mode = RetrievalSearchResponse['mode']
type Selection = { repo: Repository; snapshot: SnapshotItem }
type SearchResult = { data: RetrievalSearchResponse | null; error: ApiError | null }
const MODES: readonly Mode[] = ['keyword', 'semantic', 'hybrid']
const LABELS: Record<Mode, string> = { keyword: '關鍵字', semantic: '語意', hybrid: '混合' }
const INDEX_LABELS: Record<RetrievalIndex['status'], string> = {
  none: '尚未建立',
  building: '建立中',
  failed: '建立失敗',
  completed: '已完成',
}

function SnapshotPicker({
  repo, selected, reloadKey, onToggle,
}: {
  repo: Repository
  selected: Selection[]
  reloadKey: number
  onToggle: (repo: Repository, snapshot: SnapshotItem) => void
}) {
  const snapshots = useSnapshotList(repo.id, 'completed', reloadKey)
  return (
    <div>
      {snapshots.loading && <p role="status">載入版本中…</p>}
      <ErrorBox error={snapshots.error} />
      {snapshots.truncated && <p className="warning">只載入最近 2,000 個版本。</p>}
      {snapshots.items?.length === 0 && <p className="empty">這個 repo 沒有已匯入的版本。</p>}
      <ul className="retrieval-snapshots">
        {snapshots.items?.map((snapshot) => {
          const checked = selected.some((item) => item.snapshot.id === snapshot.id)
          return (
            <li key={snapshot.id}>
              <label className="check-label">
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={!checked && selected.length >= 20}
                  onChange={() => onToggle(repo, snapshot)}
                />{' '}
                快照 #{snapshot.id}・<code>{snapshot.commit_sha.slice(0, 12)}</code>
                {snapshot.is_stale_profile && '（匯入設定已變更）'}
              </label>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

const DEVICE_LABELS: Record<EmbeddingDevice, string> = { cpu: 'CPU', gpu: 'GPU' }

/** 工作 params 是不可信內容：只接受已知的兩個值。 */
function jobDevice(params: Record<string, unknown> | undefined): EmbeddingDevice | null {
  const value = params?.device
  return value === 'cpu' || value === 'gpu' ? value : null
}

/** GPU 為什麼不能選；可以選時回傳 null。 */
function gpuUnavailableReason(devices: EmbeddingDevices | null, failed: boolean): string | null {
  if (devices === null) return failed ? '無法取得裝置狀態（查詢橋接沒有回應）' : '正在檢查…'
  if (!devices.gpu.configured) return '尚未設定 GPU 服務'
  if (!devices.gpu.available) return 'GPU 服務沒有啟動或沒有回應（見 README「選用：使用 NVIDIA GPU」）'
  return null
}

function DevicePicker({
  device, onChange, reason,
}: {
  device: EmbeddingDevice
  onChange: (device: EmbeddingDevice) => void
  reason: string | null
}) {
  return (
    <fieldset className="device-picker">
      <legend>建立索引使用的運算裝置</legend>
      <label className="check-label">
        <input type="radio" name="embedding-device" checked={device === 'cpu'} onChange={() => onChange('cpu')} />{' '}
        {DEVICE_LABELS.cpu}
      </label>{' '}
      <label className="check-label">
        <input
          type="radio"
          name="embedding-device"
          checked={device === 'gpu'}
          disabled={reason !== null && device !== 'gpu'}
          onChange={() => onChange('gpu')}
        />{' '}
        {DEVICE_LABELS.gpu}
      </label>
      {reason !== null && <p className={device === 'gpu' ? 'warning' : 'muted'}>GPU 目前不可用：{reason}。{device === 'gpu' && '請改選 CPU。'}</p>}
      <p className="note">只影響速度，不影響索引內容與搜尋；已建立的索引在兩種裝置間通用。不會自動改用另一種裝置。</p>
    </fieldset>
  )
}

function RetrievalIndexRow({
  selection, device, blocked, onRemove,
}: {
  selection: Selection
  device: EmbeddingDevice
  blocked: boolean
  onRemove: () => void
}) {
  const [reload, setReload] = useState(0)
  const action = useAction()
  const id = selection.snapshot.id
  const state = usePolling<RetrievalIndex>(`/api/v1/snapshots/${id}/retrieval-index`, {
    enabled: true,
    reloadKey: reload,
    intervalMs: (data) => (data?.status === 'building' || (data?.job && !isFinished(data.job)) ? 2_000 : 30_000),
  })
  const active = state.data?.job !== null && state.data?.job !== undefined && !isFinished(state.data.job)
  const usedDevice = jobDevice(state.data?.job?.params)

  function build() {
    void action.run(async () => {
      const result = await api<JobSubmitResponse>('POST', `/api/v1/snapshots/${id}/retrieval-jobs`, { body: { device } })
      setReload((value) => value + 1)
      return result.data.reused ? '目前設定的檢索索引已完成。' : '已提交背景工作。'
    })
  }

  return (
    <li className="retrieval-index-row">
      <strong>{repoLabel(selection.repo)}</strong>・快照 #{id}・<code>{selection.snapshot.commit_sha}</code>
      <button type="button" className="secondary small" onClick={onRemove}>移除選取</button>
      {state.loading && <p role="status">載入索引狀態…</p>}
      <PollErrorBox state={state} />
      {state.data && (
        <div>
          索引：{INDEX_LABELS[state.data.status]}
          {state.data.chunk_count !== null && `・${state.data.chunk_count} 個片段／${state.data.segment_count} 個向量`}
          {state.data.job && `・工作 #${state.data.job.id} ${JOB_STATUS_LABEL[state.data.job.status]}`}
          {state.data.job && usedDevice && `・裝置 ${DEVICE_LABELS[usedDevice]}`}
        </div>
      )}
      {state.data?.job && active && <JobProgress job={state.data.job} />}
      {state.data?.job && (state.data.job.status === 'failed' || state.data.job.status === 'cancelled') && (
        <ErrorBox error={jobFailure(state.data.job)} />
      )}
      <button
        type="button"
        disabled={action.busy || active || blocked || state.loading || state.error !== null || state.data?.status === 'completed'}
        onClick={build}
      >
        {state.data?.status === 'failed' ? '重試建立檢索索引' : '建立檢索索引'}（{DEVICE_LABELS[device]}）
      </button>
      {action.notice && <p className="success" role="status">{action.notice}</p>}
      <ErrorBox error={action.error} />
    </li>
  )
}

function Results({ mode, result, selected }: { mode: Mode; result: SearchResult; selected: Selection[] }) {
  return (
    <section className="retrieval-result">
      <h4>{LABELS[mode]}搜尋</h4>
      <ErrorBox error={result.error} />
      {result.data && (
        <>
          <p role="status">{result.data.items.length === 0 ? '沒有符合結果。' : `${result.data.items.length} 筆結果`}</p>
          <ol className="search-results">
            {result.data.items.map((item) => {
              const repo = selected.find((entry) => entry.repo.id === item.repository_id)?.repo
              return (
                <li className="search-hit" key={item.chunk_id}>
                  <div>{repo ? repoLabel(repo) : `repo #${item.repository_id}`}・快照 #{item.snapshot_id}</div>
                  <div><code className="wrap">{item.commit_sha}</code></div>
                  <div><code className="wrap">{item.path}:{item.start_line}–{item.end_line}</code></div>
                  <div className="muted">
                    {item.support_level === 'structure' ? '結構輔助切分' : '文字切分'}
                    {item.keyword_score !== undefined && `・關鍵字 ${item.keyword_score}`}
                    {item.semantic_score !== undefined && `・相似度 ${item.semantic_score.toFixed(4)}`}
                    {item.hybrid_score !== undefined && `・融合 ${item.hybrid_score.toFixed(4)}`}
                  </div>
                  <pre className="retrieval-content">{item.content}</pre>
                </li>
              )
            })}
          </ol>
        </>
      )}
    </section>
  )
}

export function RetrievalPanel({ repos, snapshotKey }: { repos: Repository[]; snapshotKey: number }) {
  const [open, setOpen] = useState(false)
  const [repoId, setRepoId] = useState('')
  const [selected, setSelected] = useState<Selection[]>([])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<Record<Mode, SearchResult> | null>(null)
  const [busy, setBusy] = useState(false)
  const [device, setDevice] = useState<EmbeddingDevice>('cpu')
  const [scope, setScope] = useState<SearchScope>(DEFAULT_SCOPE)
  const scopeFields = scopeBody(scope)
  const devices = usePolling<EmbeddingDevices>('/api/v1/retrieval/devices', {
    enabled: open,
    intervalMs: () => 10_000,
  })
  const gpuReason = gpuUnavailableReason(devices.data, devices.error !== null)
  const blocked = device === 'gpu' && gpuReason !== null
  const controller = useRef<AbortController | null>(null)
  const repo = repos.find((item) => String(item.id) === repoId)

  useEffect(() => () => controller.current?.abort(), [])

  function clearSearch() {
    controller.current?.abort()
    controller.current = null
    setBusy(false)
    setResults(null)
  }

  function toggle(repoItem: Repository, snapshot: SnapshotItem) {
    clearSearch()
    setSelected((current) =>
      current.some((item) => item.snapshot.id === snapshot.id)
        ? current.filter((item) => item.snapshot.id !== snapshot.id)
        : current.length < 20 ? [...current, { repo: repoItem, snapshot }] : current,
    )
  }

  async function compare() {
    if (controller.current !== null || selected.length === 0 || !query.trim() || scopeFields === null) return
    clearSearch()
    const request = new AbortController()
    controller.current = request
    setBusy(true)
    async function run(mode: Mode): Promise<SearchResult> {
      try {
        const response = await api<RetrievalSearchResponse>('POST', '/api/v1/retrieval/search', {
          body: { snapshot_ids: selected.map((item) => item.snapshot.id), query, mode, limit: 10, ...scopeFields },
          signal: request.signal,
        })
        return { data: response.data, error: null }
      } catch (error) {
        return { data: null, error: isAbortError(error) ? null : toApiError(error) }
      }
    }
    const [keyword, semantic, hybrid] = await Promise.all([
      run('keyword'), run('semantic'), run('hybrid'),
    ])
    if (!request.signal.aborted) {
      setResults({ keyword, semantic, hybrid })
      setBusy(false)
      controller.current = null
    }
  }

  return (
    <section className="panel">
      <details open={open} onToggle={(event) => {
        const next = event.currentTarget.open
        setOpen(next)
        if (!next) clearSearch()
      }}>
        <summary>跨 repo 檢索與搜尋比較</summary>
        {open && (
          <div className="detail">
            <ApiHint apis={['GET /api/v1/retrieval/devices', 'GET /api/v1/snapshots/{id}/retrieval-index', 'POST /api/v1/snapshots/{id}/retrieval-jobs', 'POST /api/v1/retrieval/search']} />
            <p className="note">選擇最多 20 個已匯入版本，建立各自的檢索索引後比較三種搜尋。語意與混合搜尋需要本機模型服務及查詢橋接。</p>
            <DevicePicker device={device} onChange={setDevice} reason={gpuReason} />
            <label>
              選擇 repo{' '}
              <select value={repoId} onChange={(event) => setRepoId(event.target.value)}>
                <option value="">請選擇</option>
                {repos.map((item) => <option key={item.id} value={item.id}>{repoLabel(item)}</option>)}
              </select>
            </label>
            {repo && <SnapshotPicker key={repo.id} repo={repo} selected={selected} reloadKey={snapshotKey} onToggle={toggle} />}
            <h4>已選版本（{selected.length}/20）</h4>
            {selected.length === 0 && <p className="empty">尚未選取版本。</p>}
            <ul className="retrieval-indexes">
              {selected.map((item) => (
                <RetrievalIndexRow
                  key={item.snapshot.id}
                  selection={item}
                  device={device}
                  blocked={blocked}
                  onRemove={() => toggle(item.repo, item.snapshot)}
                />
              ))}
            </ul>
            <form onSubmit={(event) => { event.preventDefault(); void compare() }}>
              <SearchScopePicker scope={scope} onChange={(next) => { clearSearch(); setScope(next) }} />
              <label>
                查詢{' '}
                <input
                  type="text"
                  value={query}
                  maxLength={512}
                  onChange={(event) => { clearSearch(); setQuery(event.target.value) }}
                />
              </label>{' '}
              <button type="submit" disabled={busy || selected.length === 0 || !query.trim() || scopeFields === null}>
                {busy ? '搜尋中…' : '比較三種搜尋'}
              </button>
            </form>
            {busy && <p role="status">搜尋中，請稍候…</p>}
            {results && <div className="retrieval-results">{MODES.map((mode) => (
              <Results key={mode} mode={mode} result={results[mode]} selected={selected} />
            ))}</div>}
          </div>
        )}
      </details>
    </section>
  )
}
