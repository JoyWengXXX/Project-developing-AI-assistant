// 背景工作的顯示對照表與純函式（只負責「怎麼顯示」；狀態機與重試規則都在後端，前端不重做）。

import { ERROR_HINT, formatTime } from './format'
import { githubRepoOf, githubSucceededText } from './githubJobs'
import type { Job, JobKind, JobPhase, JobProgress, JobsStatus, JobStatus, JobStatusFilter, ServiceStatus } from './types'

export const JOB_KIND_LABEL: Record<JobKind, string> = {
  snapshot_import: '匯入版本',
  snapshot_index: '建立索引',
  snapshot_retrieval: '建立檢索索引',
  github_register: '註冊 GitHub repo',
  github_fetch: '更新 GitHub repo',
}

/** 卡片與詳情提示的用語：「背景匯入進行中」「上次背景更新失敗」。 */
export const JOB_BACKGROUND_LABEL: Record<JobKind, string> = {
  snapshot_import: '背景匯入',
  snapshot_index: '背景索引',
  snapshot_retrieval: '背景檢索索引',
  github_register: '背景註冊',
  github_fetch: '背景更新',
}

// 取消的說明：名詞、取消後的狀況、取消生效前的等待（依工作種類，三處共用：取消按鈕、工作列表、卡片提示）
const CANCEL_NOUN: Record<JobKind, string> = {
  snapshot_import: '匯入',
  snapshot_index: '背景工作',
  snapshot_retrieval: '檢索索引',
  github_register: '註冊',
  github_fetch: '更新',
}
const AFTER_CANCEL: Record<JobKind, string> = {
  snapshot_import: '之後可以重新匯入。',
  snapshot_index: '之後可以重新執行。',
  snapshot_retrieval: '之後可以重新執行。',
  github_register: '之後可以重新註冊。',
  github_fetch: '之後可以再更新。',
}
const CANCEL_CONSEQUENCE: Record<JobKind, string> = {
  snapshot_import: '會停止這次匯入；已讀取的內容不會保留，之後可以重新匯入。',
  snapshot_index: '會停止這個工作；已完成的部分不會保留，之後可以重新執行。',
  snapshot_retrieval: '會停止建立檢索索引；未完成的結果不會公開，之後可以重新執行。',
  github_register: '會停止這次複製，已下載的暫存內容會被清除；之後可以重新註冊。',
  github_fetch: '會停止這次更新（fetch）；本機的 clone 保持原狀，之後可以再更新。',
}

/** 「確定要取消這次○○嗎？」的○○：匯入、註冊、更新；其他種類用「背景工作」。 */
export function cancelNoun(kind: JobKind): string {
  return CANCEL_NOUN[kind]
}

/** 取消對話的說明：取消會發生什麼事。 */
export function cancelConsequence(kind: JobKind): string {
  return CANCEL_CONSEQUENCE[kind]
}

/** 工作列表的取消確認說明：排隊中立即取消；執行中則依種類說明（匯入等批次處理完，GitHub 工作會終止 git 程序）。 */
export function cancelExplain(job: Job): string {
  if (job.status === 'queued') return `排隊中的工作會立即取消；${AFTER_CANCEL[job.kind]}`
  if (job.kind === 'snapshot_import') {
    return '執行中的工作會先記錄取消請求，等目前這一批處理完才會停止；已讀取的內容不會保留，之後可以重新匯入。'
  }
  return CANCEL_CONSEQUENCE[job.kind]
}

/** 已要求取消、執行中的工作還沒停下來時的說明：匯入要等目前這一批，GitHub 工作約數秒內停止。 */
export function cancellingWait(kind: JobKind): string {
  return kind === 'github_register' || kind === 'github_fetch' ? '約數秒內就會停止' : '等目前這一批處理完就會停止'
}

export const JOB_STATUS_LABEL: Record<JobStatus, string> = {
  queued: '排隊中',
  running: '執行中',
  succeeded: '已完成',
  failed: '失敗',
  cancelled: '已取消',
}

// 徽章的顏色沿用既有 .badge-*；文字永遠同時顯示，不單靠顏色分辨
export const JOB_STATUS_BADGE: Record<JobStatus, string> = {
  queued: 'badge-neutral',
  running: 'badge-importing',
  succeeded: 'badge-completed',
  failed: 'badge-failed',
  cancelled: 'badge-neutral',
}

export const JOB_PHASE_LABEL: Record<JobPhase, string> = {
  pending: '等待中',
  importing: '匯入檔案',
  indexing: '建立索引',
  embedding: '建立檢索索引',
  resolving: '連線 GitHub 並確認 repo',
  cloning: '複製 repo 中',
  registering: '寫入資料庫',
  fetching: '更新（fetch）中',
  completed: '已完成',
}

/** 完整列表專用的篩選值「進行中與失敗」：後端沒有這個值，用頁面共用的 active、failed 兩份輪詢結果合併顯示（見 useJobsOverview）。 */
export const JOBS_ATTENTION = 'attention'
export type JobsFilterValue = JobStatusFilter | '' | typeof JOBS_ATTENTION

// 狀態篩選：除了 attention，value 直接對應後端 status 查詢參數；預設是「進行中與失敗」
export const JOB_STATUS_FILTERS: { value: JobsFilterValue; label: string }[] = [
  { value: JOBS_ATTENTION, label: '進行中與失敗' },
  { value: '', label: '全部（含已完成）' },
  { value: 'active', label: '只看進行中（排隊＋執行）' },
  { value: 'failed', label: '只看失敗' },
  { value: 'succeeded', label: '只看已完成' },
  { value: 'cancelled', label: '只看已取消' },
]

export const SERVICE_STATUS_LABEL: Record<ServiceStatus, string> = {
  online: '在線',
  stale: '逾時',
  absent: '未啟動',
}

export const SERVICE_STATUS_BADGE: Record<ServiceStatus, string> = {
  online: 'badge-completed',
  stale: 'badge-importing',
  absent: 'badge-neutral',
}

export const SERVICE_NAME: Record<'dispatcher' | 'worker', string> = {
  dispatcher: 'dispatcher（派送器）',
  worker: 'worker（執行器）',
}

export type JobAction = 'cancel' | 'retry'

export const JOB_ACTION_LABEL: Record<JobAction, string> = { cancel: '取消', retry: '重試' }

/**
 * 進度百分比（0～100 的整數）；總數未知（null）或為 0 時回傳 null——不偽造百分比。
 * 用無條件捨去：還沒做完時不會顯示成 100%。
 */
export function progressPercent(progress: Pick<JobProgress, 'processed_files' | 'total_files'>): number | null {
  const { processed_files: done, total_files: total } = progress
  if (total === null || total <= 0) return null
  return Math.max(0, Math.min(100, Math.floor((done / total) * 100)))
}

/** 工作正在（或最後停在）建立索引：匯入工作的 indexing 階段，或整個 snapshot_index 工作。 */
export function isIndexing(job: Job): boolean {
  return job.phase === 'indexing' || (job.kind === 'snapshot_index' && job.status === 'running')
}

/**
 * 目前階段的進度數字：索引階段是「已建立索引的檔案／可索引的檔案總數」（後端在進入索引階段時把
 * total_files 改成可索引的檔案數）；其他階段是「已處理／總檔案數」。
 */
export function progressNumbers(job: Job): { done: number; total: number | null } {
  const p = job.progress
  return isIndexing(job) ? { done: p.indexed_files, total: p.total_files } : { done: p.processed_files, total: p.total_files }
}

/** 同 progressPercent，但依工作所在的階段選用數字。 */
export function jobPercent(job: Job): number | null {
  const { done, total } = progressNumbers(job)
  return progressPercent({ processed_files: done, total_files: total })
}

/**
 * clone 期間的「已接收」說明；bytes_received 為 null（不在 clone 期間，或 worker 還沒量到）時回傳 null，不顯示 0。
 * 數字是暫存目錄的大小，不是百分比（git clone 不提供百分比）。
 */
export function bytesReceivedText(bytes: number | null): string | null {
  return bytes === null ? null : `已接收約 ${formatSize(bytes)}（暫存目錄大小，非百分比）`
}

/** 毫秒 → 「3 分 05 秒」這類短字串（負數視為 0）。 */
export function formatDuration(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000))
  const days = Math.floor(totalSeconds / 86400)
  const hours = Math.floor((totalSeconds % 86400) / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (days > 0) return `${days} 天 ${hours} 小時`
  if (hours > 0) return `${hours} 小時 ${String(minutes).padStart(2, '0')} 分`
  if (minutes > 0) return `${minutes} 分 ${String(seconds).padStart(2, '0')} 秒`
  return `${seconds} 秒`
}

/** 距離某個時間點還有幾毫秒（已過去回傳 0）；iso 為 null 或無法解析時回傳 null。 */
export function remainingMs(iso: string | null, nowMs: number): number | null {
  if (iso === null) return null
  const target = Date.parse(iso)
  if (Number.isNaN(target)) return null
  return Math.max(0, target - nowMs)
}

/** 兩個 ISO 時間之間的毫秒數；任一為 null 或無法解析時回傳 null。 */
export function durationBetween(fromIso: string | null, toIso: string | null): number | null {
  if (fromIso === null || toIso === null) return null
  const from = Date.parse(fromIso)
  const to = Date.parse(toIso)
  if (Number.isNaN(from) || Number.isNaN(to)) return null
  return to - from
}

/** 位元組 → 約略大小（KB／MB／GB，一位小數）。 */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let i = 0
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024
    i += 1
  }
  return `${value.toFixed(1)} ${units[i]}`
}

/** 目前這個 job 是否能顯示「取消」「重試」；最終仍以後端回應（409／404）為準。 */
export function canCancel(job: Job): boolean {
  return job.status === 'queued' || job.status === 'running'
}

/** 已要求取消、但執行中的工作還沒在檢查點停下來（最多延遲一批）：顯示「取消中…」。 */
export function isCancelling(job: Job): boolean {
  return job.status === 'running' && job.cancel_requested
}

/** 取消的時間：優先用使用者要求取消的時間，舊版後端沒有這個欄位時退回結束時間。 */
export function cancelTimeIso(job: Job): string | null {
  return job.cancel_requested_at ?? job.finished_at
}

/** worker 在處理這個工作時中斷的總次數（舊版後端沒有 diagnostics 時為 0）。 */
export function workerCrashCount(job: Job): number {
  return job.diagnostics?.poison?.total_crashes ?? 0
}

/** 有失敗追查資訊可顯示（JobFailureInfo 會輸出內容）：上一次（或這一次）的錯誤，或 worker 曾中斷。 */
export function hasFailureDetails(job: Job): boolean {
  return job.error !== null || workerCrashCount(job) > 0
}

/** 重試額度已耗盡：失敗原因是 worker_lost（租約過期且沒有剩餘的重試次數）。 */
export function isRetryExhausted(job: Job): boolean {
  return job.status === 'failed' && job.error?.code === 'worker_lost'
}

/**
 * 匯入正在列舉檔案：後端還沒回報檔案總數（total_files 為 null）、也還沒處理任何檔案。
 * 這段時間沒有百分比可說，只能顯示「正在列舉檔案…」。
 */
export function isEnumerating(job: Job): boolean {
  return job.kind === 'snapshot_import' && job.status === 'running' && job.progress.total_files === null && job.progress.processed_files === 0
}

/** 執行中的批次進度「第 3／21 批」（只有匯入的 blob 批次，次要資訊）；沒有就是 null。 */
export function batchText(job: Job): string | null {
  const unit = job.diagnostics?.current_unit
  if (job.status !== 'running' || !unit || unit.kind !== 'blob_batch' || unit.index === null || unit.count === null) return null
  return `第 ${Math.min(unit.index + 1, unit.count)}／${unit.count} 批`
}

/** 進度數字的單位：檢索索引工作的 processed_files／total_files 是「片段」，其餘是「檔案」。 */
function progressUnit(job: Job): string {
  return job.kind === 'snapshot_retrieval' ? '片段' : '檔案'
}

/** 進度文字（進度條旁）：百分比與「已處理 N / M 個檔案」；總數未知時不偽造百分比。 */
export function progressText(job: Job): string {
  const { done, total } = progressNumbers(job)
  const pct = jobPercent(job)
  const verb = isIndexing(job) ? '已建立索引' : '已處理'
  const unit = progressUnit(job)
  if (pct !== null && total !== null) return `${pct}%，${verb} ${done.toLocaleString('zh-TW')} / ${total.toLocaleString('zh-TW')} 個${unit}`
  if (total === 0) return isIndexing(job) ? '沒有可建立索引的檔案（目前只支援 Python）' : `沒有${unit}需要處理（${done} / 0）`
  if (isEnumerating(job)) return '正在列舉檔案…'
  if (done > 0) return `${verb} ${done.toLocaleString('zh-TW')} 個${unit}（總數未知）`
  if (job.status === 'running') return '進行中（總數未知，無法估計進度）'
  if (job.status === 'queued') return '尚未開始'
  return '沒有進度資料'
}

/**
 * 檢索索引工作的「預估剩餘時間」說明；後端資料不足（太早、尚未處理）時為 null，不偽造。
 * 這是以本次執行的平均速率線性外推的估計，片段大小與模型速度都會變。
 */
export function estimateText(job: Job): string | null {
  const seconds = job.progress.estimated_remaining_seconds
  if (job.status !== 'running' || seconds === null || seconds === undefined) return null
  return `預估剩餘約 ${formatDuration(seconds * 1000)}（估計值，實際速度會變）`
}

export function canRetry(job: Job): boolean {
  return job.status === 'failed'
}

/**
 * 「有工作在排隊，但執行它們的服務不在線」的提示；沒有需要提示時回傳 null。
 * 文案只描述偵測結果：第二階段切片 2 還沒有 worker 程式，不宣稱 worker 已可用。
 */
export function queueWarning(status: JobsStatus): string | null {
  if (status.queued <= 0) return null
  const { worker, dispatcher } = status
  if (worker.status !== 'online') {
    const why =
      worker.status === 'stale'
        ? `但 worker 的心跳已超過 ${status.heartbeat_timeout_seconds} 秒沒有更新`
        : '但沒有偵測到 worker'
    return `有 ${status.queued} 個工作在排隊，${why}，排隊中的工作目前不會被執行。`
  }
  if (dispatcher.status !== 'online') {
    const why = dispatcher.status === 'stale' ? '但 dispatcher 的心跳已逾時' : '但沒有偵測到 dispatcher'
    return `有 ${status.queued} 個工作在排隊，${why}，工作可能不會被派送給 worker。`
  }
  return null
}

// ---------- 白話摘要（工作列表給一般使用者看的文字；階段、嘗試次數等術語只留在「詳情」） ----------

/** params／result 是不可信內容：只取符合 commit SHA 格式的字串，並縮成 8 碼。 */
function commitShaOf(job: Job): string | null {
  for (const source of [job.params, job.result]) {
    const value = source?.commit_sha
    if (typeof value === 'string' && /^[0-9a-f]{7,64}$/i.test(value)) return value.slice(0, 8)
  }
  return null
}

/**
 * 一句話說明這個工作在做什麼，例如「匯入 deds-portal 的版本 be6ab7df」。
 * repoName：已載入的 repo 列表中的名稱，對不到時呼叫端傳 `#id`；工作沒有 repo 時傳 null。
 */
export function jobTitle(job: Job, repoName: string | null): string {
  const repo = repoName ?? '（未指定 repo）'
  const sha = commitShaOf(job)
  switch (job.kind) {
    case 'snapshot_import':
      return sha !== null ? `匯入 ${repo} 的版本 ${sha}` : `匯入 ${repo} 的版本`
    case 'snapshot_index':
      return sha !== null ? `為 ${repo} 的版本 ${sha} 建立索引` : `為 ${repo} 建立索引`
    case 'snapshot_retrieval':
      return sha !== null ? `為 ${repo} 的版本 ${sha} 建立檢索索引` : `為 ${repo} 建立檢索索引`
    case 'github_register': {
      const target = githubRepoOf(job) ?? repoName
      return target !== null ? `註冊 GitHub repo ${target}` : '註冊 GitHub repo'
    }
    case 'github_fetch':
      return repoName !== null ? `更新 ${repoName} 的 GitHub 內容` : '更新 GitHub repo 的內容'
  }
}

/** 「已重試 N 次」：只有發生過重試（嘗試次數大於 1）才有值。 */
export function retriedText(job: Job): string | null {
  return job.attempt > 1 ? `已重試 ${job.attempt - 1} 次` : null
}

/** 工作的結果句：進行中、排隊中、已完成（檔案數與耗時）、失敗（繁中原因）、已取消。 */
export function jobOutcome(job: Job, options: { workerOffline: boolean }): string {
  const files = job.progress.processed_files
  switch (job.status) {
    case 'queued':
      if (options.workerOffline) return '排隊中（背景執行器目前不在線，暫時不會執行）'
      if (job.kind === 'snapshot_retrieval' && job.progress.processed_files > 0 && job.progress.total_files !== null) {
        // 時間切片之間（或失敗後重試前）：已完成的片段已保存，下一次從這裡接著做
        return `排隊中（已完成 ${job.progress.processed_files.toLocaleString('zh-TW')} / ${job.progress.total_files.toLocaleString('zh-TW')} 個片段，等待繼續）`
      }
      return job.error !== null ? '排隊中（上次嘗試失敗，等待自動重試）' : '排隊中（等待背景執行器）'
    case 'running': {
      if (job.cancel_requested) return `取消中…（${cancellingWait(job.kind)}）`
      if (isIndexing(job)) return `進行中：建立程式結構索引（${progressText(job)}）`
      if (job.kind !== 'snapshot_import') {
        // 註冊 clone 期間才有 bytes_received（其他階段與其他種類為 null，不顯示）
        const received = bytesReceivedText(job.progress.bytes_received)
        const detail = job.kind === 'snapshot_retrieval' && job.progress.total_files !== null ? `（${progressText(job)}）` : ''
        return `進行中：${JOB_PHASE_LABEL[job.phase]}${detail}${received !== null ? `，${received}` : ''}`
      }
      if (isEnumerating(job)) return '進行中：正在列舉檔案…'
      return files > 0 ? `進行中：已處理 ${files.toLocaleString('zh-TW')} 個檔案` : '進行中：正在讀取版本內容'
    }
    case 'succeeded': {
      const parts = [githubSucceededText(job) ?? '已完成']
      const total = files > 0 ? files : (job.progress.total_files ?? 0) // 索引階段會把 total_files 改成可索引的檔案數，匯入的檔案數看 processed_files
      if (job.kind === 'snapshot_import' && total > 0) parts.push(`共 ${total.toLocaleString('zh-TW')} 個檔案`)
      const ms = durationBetween(job.started_at, job.finished_at)
      if (ms !== null) parts.push(ms < 1000 ? '耗時不到 1 秒' : `耗時 ${formatDuration(ms)}`)
      return parts.join('，')
    }
    case 'failed':
      return `失敗：${failureReason(job)}`
    case 'cancelled':
      return '已取消'
  }
}

/** 進行中工作的一小段進度說明（括號內用）：「排隊中」「42%，已處理 1,200 / 3,000 個檔案」「正在列舉檔案…」「取消中…」。 */
export function progressSummary(job: Job, options: { workerOffline: boolean }): string {
  if (job.status === 'queued') return options.workerOffline ? '排隊中，背景執行器離線' : '排隊中'
  if (isCancelling(job)) return '取消中…'
  const { done } = progressNumbers(job)
  if (jobPercent(job) !== null || isEnumerating(job) || done > 0) return progressText(job)
  return '進行中'
}

/** 失敗原因（繁中）：優先用 ERROR_HINT 的白話說明，沒有就退回後端的 message；原始 code 留在「詳情」。 */
export function failureReason(job: Job): string {
  if (job.error === null) return '沒有回報原因'
  return ERROR_HINT[job.error.code] ?? (job.error.message || job.error.code)
}

/** 相對時間：「剛剛」「3 分鐘前」「2 小時前」「5 天前」；超過 30 天或時間無法解析時改用日期。 */
export function relativeTime(iso: string, nowMs: number): string {
  const then = Date.parse(iso)
  if (Number.isNaN(then)) return '—'
  const seconds = Math.floor((nowMs - then) / 1000) // 時鐘稍有誤差造成的負值，一併視為「剛剛」
  if (seconds < 45) return '剛剛'
  const minutes = Math.max(1, Math.floor(seconds / 60))
  if (minutes < 60) return `${minutes} 分鐘前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小時前`
  const days = Math.floor(hours / 24)
  if (days < 30) return `${days} 天前`
  return formatTime(iso)
}
