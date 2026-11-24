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

// fetch 的結果：同步的 POST /repositories/{id}/fetch（deprecated）的回應，
// 與背景工作 github_fetch 成功後的 job.result 共用這個形狀（job 版多一個 changed_refs）。
export interface FetchResult {
  repository_id: number
  updated_refs: RefChange[]
  truncated: boolean // true：變動超過後端上限（100 筆），只回傳部分
  changed_refs?: number // 只有 job.result 有：實際變動的 ref 數（可能大於 updated_refs 的筆數）
}

// github_register 成功後的 job.result；already_registered＝worker 發現這個 repo 已經註冊過（沒有新建、沒有 clone）
export interface RegisterJobResult {
  repository_id: number
  github_repo: string
  already_registered: boolean
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

// PUT /repositories/{id}/reference 回應的 embedding 欄位（issue #25）：設定參考後排入檢索 embedding 的結果。
// job：已排入或沿用的進行中 job；completed：索引已完成；skipped：略過（reason 說明原因，參考版本仍已設定）。
export interface ReferenceEmbedding {
  status: 'job' | 'completed' | 'skipped'
  message: string
  reason: 'embedding_not_configured' | 'structure_index_pending' | null
  job: Job | null
  device: 'cpu' | 'gpu' | null // job 實際使用的裝置
  device_fallback: boolean // GPU 不可用而改用 CPU
}

export interface ReferenceSetResult extends ReferenceItem {
  embedding: ReferenceEmbedding
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

export type JobKind =
  | 'snapshot_import'
  | 'snapshot_index'
  | 'github_register'
  | 'github_fetch'
  | 'snapshot_retrieval'
  | 'qa_question'
export type JobStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
// 列表篩選多一個 active（＝queued＋running）
export type JobStatusFilter = JobStatus | 'active'
export type JobPhase =
  | 'pending'
  | 'importing'
  | 'indexing'
  | 'embedding'
  | 'resolving'
  | 'cloning'
  | 'registering'
  | 'fetching'
  | 'retrieving' // 問答（qa_question）：檢索相關片段、組上下文
  | 'generating' // 問答（qa_question）：等待模型回答並驗證引用
  | 'completed'

export interface JobProgress {
  processed_files: number
  total_files: number | null // null＝總數未知，不得偽造百分比
  indexed_files: number
  parse_error_count: number
  bytes_received: number | null // 暫存目錄大小（非百分比）
  // 檢索索引工作（snapshot_retrieval）：此時 processed_files／total_files 是「片段」數；其他種類為 null／缺少
  vectors_written?: number | null
  estimated_remaining_seconds?: number | null // 估計值，資料不足時為 null
  chunks_reused?: number | null // 本次執行以相同內容的既有向量複製完成的片段數
}

export interface JobError {
  code: string
  message: string
}

// 失敗追查用的摘要（切片 4）；不含路徑、令牌或內容
export interface JobUnit {
  kind: string // 例如 "blob_batch"（匯入：一批檔案內容）
  index: number | null // 從 0 起算
  count: number | null
}

export interface JobPoison {
  suspect_crashes: number
  skipped_units: number
  total_crashes: number // > 0＝worker 曾在處理這個工作時中斷
}

export interface JobDiagnostics {
  current_unit: JobUnit | null // 只在 running 時有值
  poison: JobPoison
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
  // 切片 4 新增的兩個欄位：舊版後端的回應沒有，所以宣告為可省略，讀取時要容錯
  cancel_requested_at?: string | null
  diagnostics?: JobDiagnostics
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

// 建立背景工作的回應（JobSubmitResponse），三個端點共用：
//   POST /repositories/{id}/import-jobs
//     202＝job 為新建、重新排入或既有的進行中 job（reused=false，snapshot_id 為 null）；
//     200＝該版本快照早已存在（reused=true，snapshot_id 有值；job 可能為 null＝快照來自同步匯入）。
//   POST /repositories/{id}/fetch-jobs
//     202＝job 為新建或既有的進行中 job（repository_id 有值）。
//   POST /github/registration-jobs
//     202＝job 為新建或既有的進行中 job（註冊成功前還沒有 repo：repository_id 為 null）；
//     200＝這個 repo 早已註冊（reused=true，job 為 null，repository_id 有值）：直接用它，不輪詢。
export interface JobSubmitResponse {
  job: Job | null
  snapshot_id: number | null
  repository_id: number | null
  reused: boolean
}

export type EmbeddingDevice = 'cpu' | 'gpu'

export interface EmbeddingDeviceState {
  configured: boolean
  available: boolean
}

export type EmbeddingDevices = Record<EmbeddingDevice, EmbeddingDeviceState>

export interface RetrievalIndex {
  snapshot_id: number
  repository_id: number
  status: IndexStatus
  index_id: number | null
  profile_hash: string
  chunk_count: number | null
  segment_count: number | null
  job: Job | null
}

export interface RetrievalSearchItem {
  chunk_id: number
  repository_id: number
  snapshot_id: number
  commit_sha: string
  path: string
  start_line: number
  end_line: number
  content: string
  split_kind: 'structure' | 'paragraph' | 'text'
  support_level: 'structure' | 'text'
  keyword_score?: number
  semantic_score?: number
  hybrid_score?: number
}

export interface RetrievalSearchResponse {
  mode: 'keyword' | 'semantic' | 'hybrid'
  items: RetrievalSearchItem[]
}

// ---------- 問答設定（第四階段 4-1c，對應 schemas.py 的 Qa* 模型） ----------

// 美元一律是字串（後端 Decimal，避免浮點誤差）；只做顯示，不在前端做金額運算
export type QaTier = 'low' | 'mid' | 'high'

export interface QaModelPrice {
  input_usd_per_million: string
  output_usd_per_million: string
}

export interface QaModelOption {
  id: string
  tier: QaTier
  price: QaModelPrice
  estimated_cost_per_question_usd: string // 假設值（見 estimate_basis），不是實測
  price_note: string | null
}

export interface QaLimits {
  per_request_tokens: number | null
  daily_tokens: number | null
  daily_cost_usd: string | null
}

export interface QaUsageTotals {
  input_tokens: number
  output_tokens: number
  total_tokens: number
  cost_usd: string
}

export interface QaUsage {
  today: QaUsageTotals
  cumulative: QaUsageTotals
  window_start: string
  window_end: string
  last_reset_at: string | null
  remaining_tokens: number | null // 沒有設定上限時為 null
  remaining_cost_usd: string | null
}

// GET／PUT /qa/settings、PUT /qa/provider-key：永遠不含 API key，只有 provider_key_configured
export interface QaSettings {
  provider: 'anthropic'
  model: string | null // 沒有預設模型，未選擇前為 null
  provider_key_configured: boolean
  provider_key_warning: string | null
  can_ask: boolean // 已選模型且 API key 可用；逐 repo 的同意另計
  limits: QaLimits
  price_table_as_of: string // YYYY-MM-DD
  estimate_basis: { input_tokens: number; output_tokens: number }
  models: QaModelOption[]
  usage: QaUsage
  updated_at: string | null
}

// PUT /qa/settings 的 body：沒送的欄位不變；model 與三個額度送 null 代表清除
export interface QaSettingsUpdate {
  model?: string | null
  per_request_tokens?: number | null
  daily_tokens?: number | null
  daily_cost_usd?: string | null
  price_table?: Record<string, QaModelPrice>
}

export interface QaConsentItem {
  repository_id: number
  consented_at: string
}

export interface QaConsentList {
  items: QaConsentItem[]
}

export interface QaConsent {
  repository_id: number
  consented: boolean
  consented_at: string | null
}

// ---------- 問答（第四階段 4-4，對應 schemas.py 的 QaQuestion* 模型） ----------
// 美元一律是字串；問題、陳述、路徑都是不可信內容：只能當文字節點顯示。

export type QaQuestionStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
export type QaAnswerStatus = 'answered' | 'insufficient_evidence'
export type QaStatementKind = 'fact' | 'inference' | 'open_question'
export type QaContextSkipReason = 'count_cap' | 'budget_exhausted' | 'empty_text'

// 依路徑判斷的檔案種類（後端 indexing/file_kinds.py）；data 與 build 不進檢索索引
export type FileKind = 'source' | 'test' | 'docs' | 'data' | 'build'

// POST /qa/questions 的 body；file_kinds／path_prefix 省略表示不篩選
export interface QaQuestionCreate {
  snapshot_ids: number[]
  question: string
  max_tokens?: number
  file_kinds?: FileKind[]
  path_prefix?: string
}

export interface QaEstimate {
  input_tokens_upper_bound: number
  max_output_tokens: number
  total_tokens_upper_bound: number
  cost_usd: string // 假設值（上限估計），不是實測
}

// POST /qa/questions 的 202 回應
export interface QaQuestionSubmitResponse {
  id: number
  status: QaQuestionStatus
  model: string
  job: Job
  estimate: QaEstimate
  status_url: string
}

// 已驗證的引用：座標取自送出的片段（行號 1 起算、含頭尾）；commit_sha 是完整 40 碼
export interface QaCitation {
  chunk_code: string
  repository_id: number
  repository: string
  snapshot_id: number
  commit_sha: string
  path: string
  start_line: number
  end_line: number
}

export interface QaStatement {
  position: number
  kind: QaStatementKind
  text: string
  citations: QaCitation[]
}

// 模型宣稱的引用（未驗證）；valid／reasons 是系統驗證的結果
export interface QaClaimedCitation {
  chunk: string
  start_line: number
  end_line: number
  valid: boolean
  reasons: string[]
}

export interface QaRejectionReason {
  reason: string
  citation_index: number | null // 引用層級的原因帶引用在該陳述中的 0 起算位置；陳述層級為 null
}

export interface QaRejectedStatement {
  position: number
  kind: QaStatementKind
  text: string
  reasons: QaRejectionReason[]
  claimed_citations: QaClaimedCitation[]
}

// code 為 null 表示這個檢索到的片段沒有送出（skipped_reason 說明原因）
export interface QaContextChunk {
  rank: number
  code: string | null
  skipped_reason: QaContextSkipReason | null
  repository_id: number
  repository: string
  snapshot_id: number
  commit_sha: string
  path: string
  start_line: number
  end_line: number
  chunk_id: number
  truncated: boolean
}

export interface QaUsageDetail {
  input_tokens: number | null // 沒有結果前為 null
  output_tokens: number | null
  usage_unknown: boolean // true＝供應商沒有回報，帳本沿用預留量
}

export interface QaCost {
  estimated_usd: string
  estimated_input_tokens: number
  estimation_source: 'provider_count' | 'local_estimate' | null // 預留輸入 token 的來源；預留前為 null
  actual_usd: string | null
  exceeded_reservation: boolean // true＝實際用量超過預留，仍以實際用量計入
}

export interface QaQuestionListItem {
  id: number
  status: QaQuestionStatus
  answer_status: QaAnswerStatus | null
  question: string
  snapshot_ids: number[]
  file_kinds: FileKind[] | null // 當次檢索範圍；null 表示沒有篩選
  path_prefix: string | null
  provider: string
  model: string
  error_code: string | null
  attempts: number // 實際呼叫供應商的次數（含重試）
  usage: QaUsageDetail
  cost: QaCost
  job_id: number | null
  created_at: string
  finished_at: string | null
}

export type QaQuestionList = Page<QaQuestionListItem>

export interface QaQuestion extends QaQuestionListItem {
  prompt_version: string
  max_tokens: number
  error: JobError | null
  provider_request_id: string | null
  duration_ms: number | null
  started_at: string | null
  statements: QaStatement[] // 只含通過驗證的陳述
  rejected_statements: QaRejectedStatement[]
  context_chunks: QaContextChunk[]
}

// DELETE /qa/questions 的 200 回應
export interface QaQuestionsDeleted {
  deleted: number
  skipped_active: number // 排隊或執行中、因此保留的問答數
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
// 422 回傳欄位錯誤；索引或快照錯誤可附狀態及快照 ID。
export interface ValidationDetail {
  loc: (string | number)[]
  msg: string
  type: string
}

// 非 422 的錯誤可附 details 物件（內容不可信，使用前須檢查型別）：
//   索引／快照錯誤：snapshot_id、index_status；search_scope_too_large：chunk_count、max_chunks；
//   token_budget_exceeded：reason（per_request_tokens／daily_tokens／daily_cost_usd）、limit、requested、used（token 為數字、美元為字串）；
//   qa_egress_not_consented：repositories（沒有同意的 repo）；
//   project_dependencies_not_found：detected_language（可能為 null）、suggestions；
//   project_answer_invalid：step（可能為 null）、options。
export interface ErrorDetailsObject {
  index_status?: string
  snapshot_id?: number
  chunk_count?: number
  max_chunks?: number
  reason?: string
  limit?: number | string
  requested?: number | string
  used?: number | string
  repositories?: { repository_id: number; name: string }[]
  detected_language?: string | null
  suggestions?: string[]
  step?: string | null
  options?: string[]
}

export type ErrorDetails = ValidationDetail[] | ErrorDetailsObject

export interface ErrorBody {
  error: {
    code: string
    message: string
    request_id: string | null
    details?: ErrorDetails
  }
}

// ---------- 程式結構索引（第二階段切片 6，對應 schemas.py 的索引模型） ----------

export type IndexStatus = 'none' | 'building' | 'completed' | 'failed'
export type ParseStatus = 'parsed' | 'parsed_with_errors' | 'failed'

export interface IndexCounts {
  files_by_language: Record<string, number>
  files_by_parse_status: Record<string, number>
  symbols: number
  symbols_by_kind: Record<string, number>
  truncated_files: number // 符號數超過單檔上限而被截斷的檔案數
}

// GET /snapshots/{id}/index：沒有索引時 200＋status="none"（不是錯誤）；只有 completed 才有 counts
export interface SnapshotIndex {
  snapshot_id: number
  status: IndexStatus
  index_id: number | null
  started_at: string | null
  completed_at: string | null
  analyzers: Record<string, unknown> // 抽取器與版本；不可信內容，只當文字顯示
  counts: IndexCounts | null
  error_summary: string | null
}

export interface IndexedFile {
  indexed_file_id: number
  file_id: number
  path: string
  language: 'python' | 'csharp' | 'typescript' | 'javascript' | 'java' | 'go' | 'rust' | 'sql'
  parse_status: ParseStatus
  error_kind: string | null
  error_count: number
  first_error: { line: number; column: number } | null // column 是 0-based UTF-8 位元組位移
  symbol_count: number
  symbols_truncated: boolean
}

export type IndexedFileList = Page<IndexedFile>

export interface SymbolSummary {
  symbol_id: number
  kind: string
  language: 'python' | 'csharp' | 'typescript' | 'javascript' | 'java' | 'go' | 'rust' | 'sql'
  name: string
  qualified_name: string
  file_id: number
  path: string
  start_line: number
  end_line: number
  parent_id: number | null
  modifiers: string | null
  is_async: boolean
  has_errors: boolean
  conditional: boolean
  has_children: boolean
}

export type SymbolList = Page<SymbolSummary>

export interface ImportReference {
  kind: string
  target: string
  imported_name: string | null
  alias: string | null
  relative_level: number | null
  line: number
}

export interface SymbolRelation {
  relation_type: string
  target_name: string
  target_symbol_id: number | null // 只有 resolved_same_file 才有值
  resolution: 'resolved_same_file' | 'ambiguous' | 'unresolved'
  line: number
}

export interface SymbolDetail extends SymbolSummary {
  start_column: number
  end_column: number
  leading_start_line: number | null
  type_parameter_count: number
  imports: ImportReference[]
  relations: SymbolRelation[]
}

// ---------- 搜尋（第二階段切片 8，對應 schemas.py 的 Search*Response） ----------

// 三個搜尋端點共用的回應外框：total 在內容搜尋為 null（只能靠 has_more 分頁）
export interface SearchPageBase<T> {
  snapshot_id: number
  repository_id: number
  commit_sha: string
  items: T[]
  total: number | null
  limit: number
  offset: number
  has_more: boolean
}

export interface FileMatch {
  file_id: number
  path: string
  byte_size: number | null
}

export interface SymbolMatch {
  symbol_id: number
  kind: string
  language: 'python' | 'csharp' | 'typescript' | 'javascript' | 'java' | 'go' | 'rust' | 'sql'
  name: string
  qualified_name: string
  file_id: number
  path: string
  start_line: number
  end_line: number
  has_errors: boolean
}

// snippet_offset／match_offset 以 Unicode 碼點計（不是 UTF-16 單位，也不是 UTF-8 位元組）
export interface TextMatch {
  file_id: number
  path: string
  line: number // 1-based
  snippet: string // 不可信內容，只能當文字顯示
  snippet_truncated: boolean
  snippet_offset: number // 片段在該行中的位移
  match_offset: number // 第一個命中在片段中的位移
}

export type FileSearchPage = SearchPageBase<FileMatch>
export type SymbolSearchPage = SearchPageBase<SymbolMatch>
export type TextSearchPage = SearchPageBase<TextMatch>

// ---------- 新專案（第五階段，對應 api/project_schemas.py） ----------

export interface ProjectConfig {
  language: string
  framework: string | null
  orm: string | null
  test_framework: string | null
  database: string | null
  dependencies: Record<string, string>
  root_path: string | null
  detected_at: string | null
  init_instructions: string | null
}

export interface ProjectInitResult {
  project_config: ProjectConfig
  confidence: 'high' | 'medium'
  manifests: string[]
  language_files: Record<string, number>
  directories: string[]
}

export interface ProjectConversationMessage {
  role: 'user' | 'assistant'
  content: string
}

export interface ProjectRecommendation {
  value: string
  reason: string | null
  source: 'reference' | 'llm'
}

export interface ProjectLlmStatus {
  state: 'used' | 'skipped' | 'failed' | 'not_needed'
  code: string | null
  model: string | null
  input_tokens: number | null
  output_tokens: number | null
}

export interface ProjectTemplate {
  name: string
  template_path: string
  files: string[]
}

export interface ProjectPlanReferenceLocation {
  path: string
  start_line: number
  end_line: number
}

export interface ProjectPlanFile {
  path: string
  module: string
  purpose: string
  complexity: 'low' | 'medium' | 'high'
  references: ProjectPlanReferenceLocation[]
}

export interface ProjectPlanDocument {
  reference_analysis: {
    detected: { language: string; framework: string | null } | null
    architecture_pattern: string
    layer_count: number | null
    core_modules: string[]
  } | null
  architecture: {
    summary: string
    layer_count: number | null
    modules: { name: string; purpose: string; depends_on: string[] }[]
  }
  files: ProjectPlanFile[]
  open_questions: string[]
  rejected_files: { path: string | null; reason: string }[]
  dropped_references: (ProjectPlanReferenceLocation & { file: string; reason: string })[]
  warnings: string[]
  workload: {
    file_count: number
    by_complexity: Record<string, number>
    input_tokens: number
    output_tokens: number
    cost_usd: string
    model: string
    note: string
  }
}

export interface ProjectPlanListItem {
  id: number
  status: 'draft' | 'confirmed'
  locked: boolean
  created_at: string
  confirmed_at: string | null
  language: string
  framework: string | null
  requirements_excerpt: string
  file_count: number
  reference: { snapshot_id: number | null; repository_name: string; commit_sha: string; available: boolean } | null
  cost_usd: string
}

export interface ProjectPlanList {
  items: ProjectPlanListItem[]
  total: number
  limit: number
  offset: number
}

export interface ProjectPlan {
  id: number
  status: 'draft' | 'confirmed'
  locked: boolean
  created_at: string
  confirmed_at: string | null
  target_project_config: ProjectConfig
  project_requirements: string
  reference: {
    snapshot_id: number | null
    repository_name: string
    commit_sha: string
    available: boolean
  } | null
  plan: ProjectPlanDocument
  plan_markdown: string
  usage: {
    provider: string
    model: string
    prompt_version: string
    input_tokens: number
    output_tokens: number
    cost_usd: string
    usage_known: boolean
  }
}

export interface ProjectCreateWithLlmResult {
  done: boolean
  step: string | null
  question: string | null
  options: string[]
  collected: Record<string, string | null>
  recommendation: ProjectRecommendation | null
  llm: ProjectLlmStatus
  project_config: ProjectConfig | null
  template: ProjectTemplate | null
  reference_note?: string | null
}