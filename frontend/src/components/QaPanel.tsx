import { useCallback, useEffect, useRef, useState } from 'react'
import { isQaActive } from '../qaQuestion'
import type { QaEstimate, QaQuestionStatus, QaQuestionSubmitResponse, Repository, SnapshotItem } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import type { JobsOverview } from '../useJobsOverview'
import type { QaConsents } from '../useQaConsents'
import type { QaSettingsState } from '../useQaSettings'
import { QaAskForm } from './QaAskForm'
import { QaHistory } from './QaHistory'
import { QaQuestionView } from './QaQuestionView'
import { QA_MAX_SNAPSHOTS, QaSnapshotPicker, type QaSelection } from './QaSnapshotPicker'

/** 「前往問答設定」：打開設定面板（<details id="qa-settings">）、捲動並把焦點移到它的標題。 */
function openSettingsPanel() {
  const panel = document.getElementById('qa-settings')
  if (!(panel instanceof HTMLDetailsElement)) return
  panel.open = true
  panel.scrollIntoView({ block: 'start' })
  panel.querySelector('summary')?.focus()
}

function SummaryBadge({ settings }: { settings: QaSettingsState }) {
  const data = settings.settings
  if (data === null) return settings.error !== null ? <span className="badge badge-failed">無法取得問答設定</span> : null
  if (data.can_ask) return <span className="badge badge-completed">可提問（{data.model}）</span>
  return <span className="badge badge-importing">{data.model === null ? '尚未選擇模型' : '尚未設定 API key'}，無法提問</span>
}

/**
 * 問答面板（第四階段 4-4）：選版本 → 提問 → 看狀態與答案 → 歷史。
 * 規則都在後端：額度夠不夠、哪個 repo 沒同意、引用有沒有效，前端只顯示伺服器的決定（錯誤依錯誤碼用繁體中文說明）。
 * 面板常駐在首頁面板區（進入 repo 詳情時只是隱藏、不卸載），所以進行中的問答不會因為換頁而遺失；
 * 收合面板會停止輪詢，再展開時從記憶體中的目前問答接著看。
 * 問題與答案只存在記憶體：不寫入 localStorage／sessionStorage／cookie／URL。
 */
export function QaPanel({
  repos, consents, settings, jobs, snapshotKey,
}: {
  repos: Repository[]
  consents: QaConsents
  settings: QaSettingsState
  jobs: JobsOverview
  snapshotKey: number
}) {
  const [open, setOpen] = useState(false)
  const [selected, setSelected] = useState<QaSelection[]>([])
  const [text, setText] = useState('')
  const [viewId, setViewId] = useState<number | null>(null)
  const [viewStatus, setViewStatus] = useState<QaQuestionStatus | null>(null)
  const [submitEstimate, setSubmitEstimate] = useState<QaEstimate | null>(null)
  const [usageKey, setUsageKey] = useState(0)
  const [historyKey, setHistoryKey] = useState(0)
  const viewRef = useRef<HTMLDivElement>(null)
  const reloadSettings = settings.reload

  // 換到另一筆問答時（送出、從歷史查看）：把焦點與捲動位置帶到檢視區
  useEffect(() => {
    if (viewId === null) return
    viewRef.current?.scrollIntoView({ block: 'nearest' })
    viewRef.current?.focus({ preventScroll: true })
  }, [viewId])

  const toggle = useCallback((repo: Repository, snapshot: SnapshotItem) => {
    setSelected((current) =>
      current.some((item) => item.snapshot.id === snapshot.id)
        ? current.filter((item) => item.snapshot.id !== snapshot.id)
        : current.length < QA_MAX_SNAPSHOTS ? [...current, { repo, snapshot }] : current,
    )
  }, [])

  // 狀態有變：額度與歷史重新讀取
  const onChanged = useCallback((status: QaQuestionStatus) => {
    setViewStatus(status)
    setUsageKey((value) => value + 1)
    setHistoryKey((value) => value + 1)
  }, [])

  function onSubmitted(response: QaQuestionSubmitResponse) {
    setSubmitEstimate(response.estimate)
    setViewStatus(response.status)
    setViewId(response.id)
    setUsageKey((value) => value + 1)
    setHistoryKey((value) => value + 1)
  }

  function view(id: number) {
    if (id === viewId) return
    setSubmitEstimate(null)
    setViewStatus(null)
    setViewId(id)
  }

  return (
    <section className="panel" id="qa-panel">
      <details
        open={open}
        onToggle={(event) => {
          const next = event.currentTarget.open
          setOpen(next)
          if (next) reloadSettings() // 展開時對齊最新的模型與 key 狀態
        }}
      >
        <summary className="summary-status">
          <span>問答</span>
          <SummaryBadge settings={settings} />
        </summary>
        {open && (
          <div className="detail">
            <ApiHint
              apis={[
                'POST /api/v1/qa/questions（提問；202）',
                'GET /api/v1/qa/questions/{id}（輪詢）',
                'GET /api/v1/qa/usage（今日額度）',
                'GET /api/v1/qa/settings',
                'GET /api/v1/snapshots/{id}/retrieval-index',
              ]}
            />
            <p className="note">
              選擇已匯入且檢索索引就緒的版本（可跨 repo、最多 {QA_MAX_SNAPSHOTS} 個）後提問。系統先檢索相關程式片段，送往供應商（需該 repo 已同意），
              再由系統自己驗證模型給的引用；沒通過驗證的陳述不會出現在答案裡。
            </p>
            <ErrorBox error={settings.error} />

            <h3>1. 選擇版本</h3>
            <QaSnapshotPicker repos={repos} selected={selected} snapshotKey={snapshotKey} onToggle={toggle} />

            <h3>2. 提問</h3>
            <QaAskForm
              settings={settings.settings}
              settingsLoading={settings.loading}
              settingsFailed={settings.error !== null}
              selected={selected}
              consents={consents}
              text={text}
              onTextChange={setText}
              usageKey={usageKey}
              onRemoveSnapshot={(item) => toggle(item.repo, item.snapshot)}
              onSubmitted={onSubmitted}
              onOpenSettings={openSettingsPanel}
            />

            <div className="qa-view" ref={viewRef} tabIndex={-1} aria-label="目前檢視的問答">
              {viewId !== null && (
                <>
                  <QaQuestionView
                    key={viewId}
                    id={viewId}
                    submitEstimate={submitEstimate}
                    jobsStatus={jobs.status}
                    onChanged={onChanged}
                  />
                  {viewStatus !== null && !isQaActive(viewStatus) && (
                    <button type="button" className="secondary small" onClick={() => setViewId(null)}>
                      關閉檢視
                    </button>
                  )}
                </>
              )}
            </div>

            <h3>3. 歷史問答</h3>
            <QaHistory
              reloadKey={historyKey}
              viewId={viewId}
              onView={view}
              onDeleted={(id) => {
                if (id === viewId) setViewId(null)
              }}
              onDeletedAll={() => {
                if (viewStatus === null || !isQaActive(viewStatus)) setViewId(null)
              }}
            />
          </div>
        )}
      </details>
    </section>
  )
}
