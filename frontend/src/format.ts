// 顯示用的小工具與對照表（只負責「怎麼顯示」，不含業務規則）。

import { ApiError } from './api'
import type { Job, ReferenceItem, Repository } from './types'

export function toApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e
  return new ApiError(null, 'unexpected_error', String(e), null)
}

/** 背景工作 failed（或 cancelled）→ 與請求錯誤相同的 ApiError，讓同一個 ErrorBox 顯示（request_id 取自 job）。 */
export function jobFailure(job: Job): ApiError {
  if (job.error !== null) return new ApiError(null, job.error.code, job.error.message, job.request_id)
  if (job.status === 'cancelled') return new ApiError(null, 'job_cancelled', '背景工作已被取消。', job.request_id)
  return new ApiError(null, 'job_failed', '背景工作失敗，但後端沒有回報原因。', job.request_id)
}

/** 參考版本所屬 repo 的顯示名稱：GitHub 來源用 owner/name，本機來源用註冊名稱。 */
export function referenceTitle(item: ReferenceItem): string {
  const r = item.repository
  return r.source_type === 'github' && r.github_owner && r.github_name ? `${r.github_owner}/${r.github_name}` : r.name
}

export function formatTime(iso: string | null): string {
  if (iso === null) return '—'
  return new Date(iso).toLocaleString('zh-TW', { hour12: false })
}

/** 讓使用者辨認 repo 的名稱：GitHub 來源用 owner/name，本機來源用註冊名稱。 */
export function repoLabel(repo: Repository): string {
  const isGitHub = repo.source_type === 'github' && repo.github_owner && repo.github_name
  return `#${repo.id} ${isGitHub ? `${repo.github_owner}/${repo.github_name}` : repo.name}`
}

/** 卡片標題：GitHub 來源用 owner/name，本機來源用註冊名稱（不含 #id）。 */
export function repoTitle(repo: Repository): string {
  const isGitHub = repo.source_type === 'github' && repo.github_owner && repo.github_name
  return isGitHub ? `${repo.github_owner}/${repo.github_name}` : repo.name
}

/** 完整 ref 名稱 → 簡名：refs/heads/main、refs/remotes/origin/main → main。 */
export function shortRefName(ref: string): string {
  for (const prefix of ['refs/heads/', 'refs/remotes/origin/', 'refs/remotes/', 'refs/']) {
    if (ref.startsWith(prefix)) return ref.slice(prefix.length)
  }
  return ref
}

/** commit SHA 只顯示前 12 碼（完整值在詳情中）。 */
export function shortSha(sha: string): string {
  return sha.slice(0, 12)
}

/** commit 圖的列內只放 8 碼（完整 SHA 在 title 與選取後的詳情中）。 */
export function commitShortSha(sha: string): string {
  return sha.slice(0, 8)
}

// 共用同一個 formatter：每次呼叫 toLocaleString 都會重新建立 Intl 物件，500 列時會明顯變慢
const commitTimeFormat = new Intl.DateTimeFormat('zh-TW', {
  hour12: false,
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
})

/** commit 時間：轉成本地時間，到分鐘（例：2026/11/10 12:30）。 */
export function formatCommitTime(iso: string): string {
  return commitTimeFormat.format(new Date(iso))
}

export function formatBytes(n: number | null): string {
  return n === null ? '—' : `${n.toLocaleString('zh-TW')} B`
}

export const SNAPSHOT_STATUS_LABEL: Record<string, string> = {
  importing: '匯入中',
  completed: '已完成',
  failed: '失敗',
}

export const FILE_STATUS_LABEL: Record<string, string> = {
  imported: '已匯入（有內容）',
  skipped: '已跳過（無內容）',
}

// 後端 snapshot_files.skip_reason 的可能值（見 docs/architecture.md「資料模型」）
export const SKIP_REASON_LABEL: Record<string, string> = {
  binary: '二進位檔，不保存內容',
  invalid_utf8: '不是有效的 UTF-8 文字，不保存內容',
  too_large: '超過單檔大小上限，未讀取內容',
  excluded: '符合排除規則（如 .env、私鑰、憑證），刻意不讀內容',
  symlink: '符號連結，不跟隨也不讀內容',
  submodule: '子模組項目，不讀內容',
  unsafe_path: '路徑不安全，不讀內容',
  unsupported: '不支援的 Git 項目類型，不讀內容',
}

// 錯誤 code 的白話說明，幫助對照後端的領域例外（src/repopilot/errors.py）
export const ERROR_HINT: Record<string, string> = {
  network_error: '瀏覽器連不到 Vite dev server。',
  non_standard_error: '回應不是統一錯誤格式；後端未啟動時，Vite proxy 會回 502／500。',
  validation_error: '請求參數不合法（FastAPI／Pydantic 驗證失敗），詳見下方 details。',
  invalid_repository_path:
    '路徑不存在、不在允許的根目錄內（本機來源：REPOSITORY_ROOTS；GitHub 來源：受管目錄），或不是 Git repo 根目錄；資料夾可能已被移動或刪除。',
  repository_not_found: '指定的 repo 不存在。',
  invalid_revision:
    '指定的分支、標籤或 commit 不存在或不合法（例如剛被 fetch 更新而消失）。請按「重新整理」更新分支清單後再試。',
  import_limit_exceeded: '這個版本的檔案數或總大小超過匯入上限，無法載入；請改選其他版本。',
  snapshot_import_in_progress:
    '另一個匯入（例如舊的同步匯入端點）正在載入這個版本，稍後再試即可（不是失敗）。',
  snapshot_not_found: '這個版本已不存在，列表會重新整理。',
  snapshot_not_completed: '這個版本還沒匯入完成（匯入中或失敗），不能設為參考版本，也不提供檔案內容。',
  snapshot_referenced: '這個版本是目前的參考版本；確認後會一併取消參考。',
  snapshot_busy: '這個版本正在匯入或有進行中的工作，請稍後再試。',
  snapshot_file_not_found: '檔案不存在，或不屬於這個快照。',
  database_unavailable: '後端連不到資料庫（請確認 docker compose 的 db 是否啟動）。',
  git_command_failed: 'Git 指令執行失敗（後端內部問題或 repo 損毀），請用 request_id 查後端日誌。',
  git_timeout:
    'Git 讀取、clone 或 fetch 逾時（歷史很大、網路慢、磁碟忙碌，或掛載目錄上 loose 物件特別多）。背景工作會先自動退避重試幾次；仍失敗時，大型 repo 可調高 GITHUB_CLONE_TIMEOUT_SECONDS，或在該 repo 自行執行 git gc 後重試（系統不會代為執行）。',
  // GitHub token（切片 5c-1）與 worker 的 token 解密（切片 5）：同一個 code，兩種情境
  token_encryption_not_configured:
    '部署設定問題：缺少 TOKEN_ENCRYPTION_KEY。註冊或更新 GitHub repo 的背景工作是 worker 缺少，請檢查 .env.worker（見 README）；儲存 token 時是後端缺少（.env），token 不會被保存，也不會送去 GitHub。',
  github_token_invalid:
    'GitHub 拒絕這個 token（無效、過期或已被撤銷）。請在「GitHub 連線」重新貼上有效的 token（無效的 token 不會被保存）；背景工作修正後可再按「重試」。',
  // GitHub repo 註冊與 fetch（切片 5c-2）
  invalid_github_repo:
    '格式不合法。請輸入 owner/repo，或 https://github.com/owner/repo；只接受 github.com，不接受空白、分號等特殊字元或 SSH 位址。',
  github_registration_disabled:
    '後端未設定 MANAGED_REPOS_DIR，GitHub 註冊與 fetch 已停用。請在 .env 設定受管目錄的絕對路徑後重啟後端。',
  github_repo_not_found_or_private:
    '找不到，或 token 沒有這個 repo 的權限；私有 repo 請先在「GitHub 連線」儲存 token（fine-grained token 須涵蓋該 repo，組織可能還需要核准）。',
  github_clone_failed: 'git clone 失敗；詳細原因只記在後端（worker）日誌，請用 request_id 對照。暫存目錄已清理，可重試。',
  github_clone_timeout: 'clone 逾時，repo 可能太大（上限由後端 GITHUB_CLONE_TIMEOUT_SECONDS 決定）。暫存目錄已清理。',
  github_unreachable: '連不上 GitHub（網路問題）。背景工作會自動退避重試；同步請求或已失敗的工作，請確認網路後再試。',
  github_api_error: 'GitHub API 回應非預期（可能是速率限制、權限禁止或 GitHub 暫時故障），請稍後重試。',
  github_timeout: '呼叫 GitHub API 逾時。背景工作會自動退避重試；同步請求或已失敗的工作，請稍後再試。',
  github_server_error: 'GitHub 暫時發生伺服器錯誤（5xx）。背景工作會自動退避重試；已失敗的工作請稍後再試。',
  repository_not_github: '這個 repo 是本機來源，只有 GitHub 來源的 repo 才能 fetch。',
  github_fetch_failed: 'git fetch 失敗（網路、token 已過期或權限不足）；詳細原因看後端（worker）日誌，確認後可重試。',
  github_fetch_timeout: 'fetch 逾時，請稍後重試。',
  managed_storage_error:
    '受管目錄的檔案系統發生問題（例如目的目錄已存在但沒有對應的資料列，需要手動處理），請用 request_id 查後端日誌。',
  repository_busy: '這個 repo 正在匯入、clone／fetch，或有背景工作進行中，暫時不能操作（含移除）；請先取消或等它完成。',
  internal_error: '後端未預期的例外，請用 request_id 查後端日誌。',
  // 背景工作（第二階段切片 2）
  request_timeout: '請求逾時，沒有收到後端回應；輪詢會自動退避並重試。',
  job_not_found: '這個工作已不存在，列表會重新整理。',
  job_not_cancellable: '這個工作目前無法取消（可能已結束）。',
  job_not_retryable: '只有失敗的工作可以重試，或同一個目標已有進行中的工作。',
  job_lease_lost: '執行這個工作的 worker 已失去它的租約（工作已被其他程序接手或已結束），請重新整理列表。',
  worker_lost: '執行背景工作的程序中途中斷，系統已自動重試；重試額度用完後才會失敗。',
  search_scope_too_large:
    '選取的版本片段總數超過關鍵字搜尋的上限（後端 SEARCH_MAX_CHUNKS，預設 5,000），關鍵字與混合搜尋預期會逾時所以沒有執行；語意搜尋不受影響。請減少選取的版本，或由部署者調高上限（可能變慢）。錯誤詳情有目前的片段總數與上限。',
  job_time_limit_exceeded:
    '背景工作超過 worker 的單一任務時限（WORKER_TASK_TIME_LIMIT_SECONDS，預設 3600 秒）而被中止，不會自動重試。大型 repo 的檢索索引（本機模型在 CPU 上約每秒處理不到 1 個向量）可能需要數小時。已完整寫入的片段會保留，按「重試」會從中斷處繼續（被打斷的那個片段會重算）；也可以暫時調高該時限。',
  profile_mismatch: 'API 與 worker 的設定雜湊不一致（兩邊的匯入或索引設定、或 Python 版本不同），請對齊設定後再重試。',
  embedding_device_unavailable:
    '選取的運算裝置（GPU）目前不可用：服務沒有啟動或沒有設定。系統不會自動改用 CPU；請啟動 embedding-gpu 服務（見 README），或改選 CPU 後再提交（已保存的片段會接續，不重算）。',
  // 程式結構索引（第二階段切片 6）
  snapshot_index_not_ready: '這個版本還沒有建立完成的程式結構索引（尚未建立、建立中或失敗）；按「建立索引」後等它完成。',
  symbol_not_found: '這個符號不存在，或不屬於這個版本目前的索引（索引可能已重建）。',
  path_not_mapped: 'repo 路徑無法映射到 worker 容器內的路徑，這是部署設定問題，請檢查 WORKER_PATH_MAPPINGS（見 README）。',
  worker_path_not_mapped:
    'repo 的主機路徑沒有對應到 worker 容器內的路徑，這是部署設定問題，請檢查 WORKER_PATH_MAPPINGS（見 README）；工作沒有被排入。',
  job_cancelled: '這個背景工作已被取消，可以重新提交（匯入按「重新匯入」、註冊按「重新註冊」、更新再按一次「更新」）。',
  job_failed: '背景工作失敗，但後端沒有回報原因；請用 request_id 查後端日誌。',
  unsupported_job_kind: 'worker 不支援這種工作（版本不一致），請確認 worker 映像與後端是同一版本。',
  invalid_response: '後端回應的內容不符合預期，請用 request_id 查後端日誌。',
  parser_crash: '某個檔案的解析器當機；該檔案以部分解析處理，不影響其他檔案。',
}

// 「移除 repo」情境專用的說明：同一個 code 在註冊與移除時含意不同（例如 managed_storage_error、
// github_registration_disabled），所以移除時優先用這張表，沒有的 code 再退回 ERROR_HINT。
export const REMOVE_ERROR_HINT: Record<string, string> = {
  repository_not_found: '這個 repo 已不存在（可能已被移除），列表會重新整理。',
  repository_busy: '有背景工作（匯入或更新）進行中，請先取消或等它完成後再移除。',
  managed_storage_error: '無法刪除本機 clone，請關閉占用該資料夾的程式後重試。',
  invalid_repository_path: '路徑與紀錄不符，為安全起見未刪除任何檔案。',
  github_registration_disabled: '請先設定 MANAGED_REPOS_DIR。',
}
