// 與後端 src/repopilot/api/schemas.py 對應的回應型別（欄位名稱與後端一致，不做轉換）。
// 時間欄位在 JSON 中是 ISO 8601 字串。

export type SourceType = 'local' | 'github'

export interface Repository {
  id: number
  name: string
  canonical_local_path: string
  created_at: string
  source_type: SourceType
  // 以下四個欄位只有 GitHub 來源才有值（local 來源皆為 null）；
  // github_is_private 在 GitHub 來源也可能是 null（舊資料或未知）
  github_owner: string | null
  github_name: string | null
  github_is_private: boolean | null
  github_default_branch: string | null
}

export interface RepositoryList {
  items: Repository[]
}

// DELETE /api/v1/repositories/{id} 的回應（200）
export interface RepositoryRemoved {
  repository_id: number
  source_type: SourceType
  deleted_snapshots: number
  deleted_files: number
  removed_clone: boolean // 只有 GitHub 來源且 clone 目錄真的被刪除時才為 true
}

// ---------- GitHub（切片 5c） ----------

export type GitHubTokenType = 'fine_grained' | 'classic' | 'unknown'

// PUT /api/v1/github/token 與 GET /api/v1/github/auth 的回應；永不含 token 本身
export interface GitHubAuth {
  authenticated: boolean
  login: string | null
  token_type: GitHubTokenType | null
  warning: string | null
}

export interface RefChange {
  name: string // 可直接當匯入的 revision，例如 origin/main、v1.0
  kind: 'branch' | 'tag'
  change: 'added' | 'updated' | 'removed'
  old_sha: string | null
  new_sha: string | null
}

// POST /api/v1/repositories/{id}/fetch 的回應
export interface FetchResult {
  repository_id: number
  updated_refs: RefChange[]
  truncated: boolean // true：變動超過後端上限（100 筆），只回傳部分
}

// ---------- 版本瀏覽（切片 5e：refs 與 commits） ----------

export interface BranchInfo {
  name: string // 顯示用短名稱（GitHub 來源已去掉 origin/）
  ref: string // 完整 ref 名稱，可直接作為 commits 的 ref 參數
  sha: string // 分支最新 commit
  is_default: boolean
}

export interface TagInfo {
  name: string
  ref: string
  sha: string // 標籤指向的 commit（附註標籤已還原）
}

// GET /api/v1/repositories/{id}/refs
export interface RefsResponse {
  default_ref: string | null // 空 repo 或 detached HEAD 時為 null
  head_sha: string | null
  branches: BranchInfo[]
  tags: TagInfo[]
  truncated: boolean // 分支或標籤超過後端上限（各 500）
}

export interface CommitRef {
  name: string
  kind: 'branch' | 'tag'
}

export interface Commit {
  sha: string // 完整 SHA
  parents: string[]
  subject: string
  author_name: string
  authored_at: string // UTC，ISO 8601
  refs: CommitRef[] // 指向這個 commit 的分支／標籤
  imported: boolean // 已有可直接使用的快照（POST /imports 會回 200）
}

// GET /api/v1/repositories/{id}/commits
export interface CommitList {
  items: Commit[]
  total_returned: number // 本頁筆數，不是總數
  has_more: boolean
}

// 後端 SnapshotResponse.status 宣告為 str，實際值由資料庫 CHECK 約束限制為這三種
export type SnapshotStatus = 'importing' | 'completed' | 'failed'
export type FileStatus = 'imported' | 'skipped'

export interface Snapshot {
  id: number
  repository_id: number
  commit_sha: string
  import_profile_hash: string
  status: SnapshotStatus
  file_count: number
  skipped_count: number
  total_bytes: number
  started_at: string
  completed_at: string | null
  error_summary: string | null
}

// GET /repositories/{id}/snapshots 的項目與 GET /snapshots/{id}（切片 3b 新增兩個欄位）。
// POST /imports 的回應仍是上面的 Snapshot（沒有這兩欄）：匯入後要重新讀取列表才拿得到。
export interface SnapshotItem extends Snapshot {
  is_referenced: boolean // 是所屬 repo 的參考版本
  is_stale_profile: boolean // 匯入設定已改變：這份快照不會被當成「目前設定」的結果
}

export interface SnapshotDetail extends SnapshotItem {
  // 只有 completed 才有統計；importing／failed 為空物件
  file_status_counts: Record<string, number>
  skip_reason_counts: Record<string, number>
}

// 分頁回應：total 是符合條件的總數，不受 limit／offset 影響
export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

// 檔案列表項目（不含 content）
export interface SnapshotFileSummary {
  id: number
  snapshot_id: number
  path: string
  git_blob_id: string | null
  sha256: string | null
  byte_size: number | null
  status: FileStatus
  skip_reason: string | null
}

// 單檔：imported 才有 content；skipped 一律為 null
export interface SnapshotFileDetail extends SnapshotFileSummary {
  content: string | null
}

// ---------- 參考版本與移除快照（切片 3b） ----------

// GET /api/v1/references 的項目，也是 PUT /repositories/{id}/reference 的回應。
// 參考只是標記（尚未連到 Agent）；快照欄位是「現在」的狀態。
export interface ReferenceItem {
  id: number
  repository: {
    id: number
    name: string
    source_type: SourceType
    github_owner: string | null
    github_name: string | null
  }
  snapshot_id: number
  commit_sha: string // 完整 SHA
  snapshot_status: SnapshotStatus
  file_count: number
  skipped_count: number
  total_bytes: number
  created_at: string
}

export interface ReferenceList {
  items: ReferenceItem[]
}

// DELETE /api/v1/snapshots/{id}?force= 的回應（200）
export interface SnapshotRemoved {
  snapshot_id: number
  repository_id: number
  deleted_files: number
  was_referenced: boolean // 移除前它是參考版本（這次用 force=true 一併取消了參考）
}

// ---------- 倉庫總覽（切片 5d：GET /api/v1/repositories/{id}/overview） ----------

export interface OverviewHead {
  sha: string // 預設分支最新 commit（完整 SHA）
  subject: string
  author_name: string
  authored_at: string // UTC，ISO 8601
}

export interface OverviewSnapshot {
  id: number
  commit_sha: string
  status: SnapshotStatus
}

export interface OverviewStats {
  imported_files: number
  skipped_files: number // 二進位、過大、被排除、symlink、submodule 等沒有內容的檔案
  total_bytes: number // 已匯入檔案的大小總和
}

export interface OverviewLanguage {
  language: string // 依副檔名對照；「其他」永遠排最後
  files: number
  bytes: number
}

export interface OverviewReadme {
  path: string
  text: string // 前 4000 字元；不可信內容，只能當純文字顯示
  truncated: boolean
}

export type ProjectInfoKind = 'pyproject' | 'requirements' | 'package_json' | 'csproj'

export interface ProjectInfo {
  kind: ProjectInfoKind
  path: string
  name: string | null
  version: string | null
  dependencies: string[] // 後端已格式化好的字串，前端只當文字顯示
  truncated: boolean
  target_frameworks: string[] // 只有 csproj 有值
  skipped_reason: string | null // 目前只有 "too_large"
  parse_error: string | null // "invalid_toml"、"invalid_json"、"not_an_object"
}

export interface RepositoryOverview {
  repository_id: number
  default_ref: string | null // 空 repo、找不到預設分支或 detached HEAD 時為 null
  head: OverviewHead | null // 空 repo 或找不到預設分支時為 null
  snapshot: OverviewSnapshot | null
  stats: OverviewStats | null // 未分析時為 null（0 個檔案與「尚未知」要能區分）
  languages: OverviewLanguage[]
  readme: OverviewReadme | null
  project_info: ProjectInfo[]
  analyzed: boolean // false：head 的最新 commit 還沒有快照；由使用者按「匯入」（走 import-jobs），不會自動匯入
}

// ---------- 背景工作（第二階段切片 2：/api/v1/jobs，對應 schemas.py 的 Job* 模型） ----------

export type JobKind = 'snapshot_import' | 'snapshot_index' | 'github_register' | 'github_fetch'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
// 列表篩選多一個 active（＝queued＋running）
export type JobStatusFilter = JobStatus | 'active'
export type JobPhase =
  | 'pending'
  | 'importing'
  | 'indexing'
  | 'resolving'
  | 'cloning'
  | 'registering'
  | 'fetching'
  | 'completed'

export interface JobProgress {
  processed_files: number
  total_files: number | null // null＝總數未知，不得偽造百分比
  indexed_files: number
  parse_error_count: number
  bytes_received: number | null // 暫存目錄大小（非百分比）
}

export interface JobError {
  code: string
  message: string
}

export interface Job {
  id: number
  kind: JobKind
  status: JobStatus
  phase: JobPhase
  repository_id: number | null
  snapshot_id: number | null
  params: Record<string, unknown> // 不可信內容：只能當文字顯示
  attempt: number
  max_attempts: number
  next_attempt_at: string | null
  cancel_requested: boolean
  progress: JobProgress
  error: JobError | null // queued 時是「上次失敗原因」
  result: Record<string, unknown> | null // 不可信內容：只能當文字顯示
  request_id: string | null
  created_at: string
  started_at: string | null
  phase_started_at: string | null
  last_heartbeat_at: string | null
  finished_at: string | null
  updated_at: string
  status_url: string
}

// POST /api/v1/repositories/{id}/import-jobs 的回應（JobSubmitResponse）：
//   202＝job 為新建、重新排入或既有的進行中 job（reused=false，snapshot_id 為 null）；
//   200＝該版本快照早已存在（reused=true，snapshot_id 有值；job 可能為 null＝快照來自同步匯入）。
export interface JobSubmitResponse {
  job: Job | null
  snapshot_id: number | null
  repository_id: number
  reused: boolean
}

// online：至少一個實例的心跳未過期；stale：有紀錄但都過期；absent：從來沒有紀錄
export type ServiceStatus = 'online' | 'stale' | 'absent'

export interface ServiceState {
  status: ServiceStatus
  last_seen_at: string | null
  instances_online: number
}

// GET /api/v1/jobs/status
export interface JobsStatus {
  dispatcher: ServiceState
  worker: ServiceState
  queued: number
  running: number
  heartbeat_timeout_seconds: number
}

export interface HealthResponse {
  status: 'ok'
}

export interface ReadyResponse {
  status: 'ready'
}

// 統一錯誤格式：{ "error": { code, message, request_id, details? } }
// details 只有 422 validation_error 才有
export interface ValidationDetail {
  loc: (string | number)[]
  msg: string
  type: string
}

export interface ErrorBody {
  error: {
    code: string
    message: string
    request_id: string | null
    details?: ValidationDetail[]
  }
}
