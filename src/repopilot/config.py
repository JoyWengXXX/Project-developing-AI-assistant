import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from repopilot.path_mapping import PathMapping

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
SqlDialect = Literal["postgres", "tsql", "mysql", "sqlite"]

DEFAULT_EXCLUDED_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "id_rsa*",
    "id_ed25519*",
    "*.p12",
    "*.pfx",
    "credentials*.json",
    ".npmrc",
    ".pypirc",
    ".netrc",
)
DEFAULT_EXCLUDED_EXCEPTIONS: tuple[str, ...] = (".env.example",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "RepoPilot"
    log_level: LogLevel = "INFO"
    # 預設值對應 docker-compose.yml 的本機開發資料庫（假密碼，僅限本機）
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://repopilot:repopilot_dev@127.0.0.1:5432/repopilot"
    )
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    # 連線池預設值與 SQLAlchemy 相同（5＋10、等待 30 秒），尚未經實測調整
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=10, ge=0, le=100)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    # 允許註冊的本機目錄；環境變數以 os.pathsep 分隔（Windows 為 ;）。預設為空＝拒絕所有註冊
    repository_roots: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    git_timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    # 匯入上限；數值未經實測，讀取 blob 內容前先依 ls-tree 的大小檢查
    import_max_file_bytes: int = Field(default=1024 * 1024, ge=1)
    import_max_files: int = Field(default=20_000, ge=1)
    import_max_snapshot_bytes: int = Field(default=200 * 1024 * 1024, ge=1)
    # 讀取整個快照 blob 的總時間上限（比單一 Git 指令的逾時長）。分批讀取（D43）時是**所有批次共用**
    # 的期限，不是每批各一份，所以最壞情況與切片 3 之前相同
    import_git_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    # blob 分批讀取（第二階段切片 4，D43）：每批最多這麼多個不同的 blob、且（依 ls-tree 的大小）
    # 合計最多這麼多位元組；每批一個 `git cat-file --batch`，批與批之間回報進度並檢查取消。
    # 只影響「怎麼切」，**不影響匯入結果**，所以不納入 import_profile_hash。單一 blob 超過位元組上限
    # 時獨占一批（它本來就受 import_max_file_bytes 限制）。數值是未實測的起點（本機 Windows 單次
    # 量測：每多一批約多 70～80 毫秒的 git 子程序啟動成本，見 D43）
    import_blob_batch_max_files: int = Field(default=200, ge=1, le=100_000)
    import_blob_batch_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1)
    # importing 超過此秒數視為程序中斷，允許後續請求接手；須大於實際匯入所需時間
    import_stale_seconds: int = Field(default=1800, ge=60)
    # 以檔名（不分大小寫、fnmatch）比對；環境變數為 JSON 陣列。納入 import_profile_hash
    import_excluded_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_PATTERNS)
    )
    import_excluded_exceptions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_EXCLUDED_EXCEPTIONS)
    )

    # Fernet 金鑰（urlsafe base64、32 位元組）；空字串＝未設定，此時不得保存 GitHub token，
    # 也沒有預設金鑰。產生方式見 README
    token_encryption_key: SecretStr = SecretStr("")
    # 僅供測試指向本機假 GitHub；正式環境維持預設
    github_api_base_url: str = "https://api.github.com"
    # 呼叫 GitHub API 的逾時（連線與讀取皆適用）；數值未經實測
    github_api_timeout_seconds: float = Field(default=10.0, gt=0, le=120)

    # GitHub clone 的目的地（受信任根目錄，與 repository_roots 分開）。須為絕對路徑；
    # 空白＝停用 GitHub 註冊。載入設定與建立 app 不會建立或寫入此目錄，使用時才建立
    managed_repos_dir: Path | None = None
    # 單次 clone／fetch 的逾時；數值未實測，大型 repo 可能需要調高
    github_clone_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    # clone 的遠端基底 URL；僅供測試指向本機 fixture，正式環境維持預設
    github_clone_base_url: str = "https://github.com"

    # --- 背景工作（第二階段切片 2，D39）---
    # 以下數值皆為規格 5.14 的「擬議起點」，**未經實測**；切片 3 起 dispatcher／worker 才會使用
    # 租約長度與心跳週期；心跳須明顯短於租約（見 _job_settings_are_consistent）
    job_lease_seconds: int = Field(default=60, ge=2, le=86_400)
    job_heartbeat_seconds: int = Field(default=15, ge=1, le=86_400)
    # 自動執行次數上限（含第一次）；手動 retry 時以「目前 attempt＋此值」作為新額度
    job_max_attempts: int = Field(default=3, ge=1, le=100)
    # 暫時性失敗的指數退避：min(max, base × 2^(n-1))，再加上 ±20% 抖動（上限仍為 max）
    job_retry_base_seconds: int = Field(default=30, ge=1, le=86_400)
    job_retry_max_seconds: int = Field(default=600, ge=1, le=86_400)
    # dispatcher 掃描待派送事件的週期（秒，可小於 1）與復原掃描週期
    dispatch_poll_seconds: float = Field(default=1.0, gt=0, le=3_600)
    recovery_scan_seconds: float = Field(default=5.0, gt=0, le=3_600)
    # 已派送但 job 仍是 queued 超過此秒數就重送（之後逐次加倍，上限 60 分鐘）
    dispatch_resend_after_seconds: int = Field(default=300, ge=1, le=86_400)
    # dispatcher／worker 寫入 service_heartbeats 的週期；超過 3 倍未更新視為離線（C8）
    service_heartbeat_seconds: int = Field(default=15, ge=1, le=3_600)

    # --- 背景工作：Celery／Redis 與 worker（第二階段切片 3，D40）---
    # broker 位址（含連線資訊，同 D14 用 SecretStr）。只有 dispatcher 與 worker 使用，
    # **API 不讀、不連 Redis**；空字串＝未設定，dispatcher／worker 啟動時會明確拒絕。
    celery_broker_url: SecretStr = SecretStr("")
    # 佇列名稱與 Redis 鍵前綴；測試堆疊必須用不同的值（規格 9.4），
    # 避免與開發堆疊共用任何 broker 資料
    celery_queue_name: str = Field(default="repopilot.jobs", pattern=r"^[A-Za-z0-9._-]{1,100}$")
    celery_global_keyprefix: str = Field(default="repopilot:", pattern=r"^[A-Za-z0-9._:-]{0,100}$")
    # 主機路徑 → 容器路徑（JSON 陣列，每項 {host, container, mode}）；只有 worker 使用，
    # API 只在建立 job 時檢查「路徑能映射」。空陣列＝沒有映射，API 建立 job 回 503
    # worker_path_not_mapped
    worker_path_mappings: Annotated[list[PathMapping], NoDecode] = Field(default_factory=list)
    # worker 的 Git 逾時（R1）：容器讀取 Windows 掛載（9p）比主機慢，loose 物件極多的 repo 尤其
    # 明顯（切片 1 實測：20,000 個 loose 物件 ls-tree 100 秒＋cat-file 291 秒，單次量測）。
    # 預設值未實測；慢的 repo 請調高，或在該 repo 自行執行 `git gc`（RepoPilot 不會替使用者的
    # repo 執行 gc）。worker 啟動時以它們取代 GIT_TIMEOUT_SECONDS／IMPORT_GIT_TIMEOUT_SECONDS
    # （API 不受影響）
    worker_git_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)
    worker_import_git_timeout_seconds: float = Field(default=900.0, gt=0, le=7_200)
    # 單一任務的硬性時間上限（超過即由 Celery 結束子行程→心跳停止→租約過期→復原掃描）；
    # 軟性上限為它減 60 秒。須大於 Git 逾時的總和，否則 Git 還沒逾時任務就先被殺
    worker_task_time_limit_seconds: int = Field(default=3_600, ge=120, le=86_400)

    # --- GitHub 註冊與 fetch 的背景化（第二階段切片 5，D44；數值皆未實測）---
    # clone／fetch 子程序執行期間，worker 每隔這麼多秒檢查一次取消旗標與租約（一次單列查詢）。
    # 取消最多延遲這麼久（加上程序樹終止的時間）
    git_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    # clone 期間統計暫存目錄位元組數、寫入 `progress.bytes_received` 的間隔（秒）
    clone_progress_seconds: float = Field(default=2.0, gt=0, le=600)
    # `.tmp/<16 位 hex>` 底下超過這麼多秒沒有被任何進行中 job 記錄、也沒有更新的目錄視為孤兒，
    # worker 啟動與每次註冊 job 開始時清理。必須大於 clone 逾時，否則同步端點（沒有 job 記錄）
    # 正在進行的 clone 可能被誤判為孤兒
    managed_temp_orphan_seconds: int = Field(default=7_200, ge=60, le=604_800)

    # --- 結構索引（第二階段切片 6；數值皆未實測）---
    # 單檔符號數上限，超過時截斷並標記 symbols_truncated（不使 job 失敗）。納入 index_profile_hash
    index_max_symbols_per_file: int = Field(default=5_000, ge=1, le=1_000_000)
    # C# 的欄位（field_declaration，每個 variable_declarator 一個符號）預設不抽取：數量大、
    # 對「看結構」的價值低。納入 index_profile_hash，切換會得到另一份索引（切片 7）
    index_csharp_include_fields: bool = False
    index_sql_dialect: SqlDialect | None = None
    index_sql_max_file_bytes: int = Field(default=20_000, ge=1, le=1_048_576)
    # 索引階段以 snapshot_files.id 的 keyset 分頁讀取：每批最多這麼多檔、且（依 byte_size）合計最多
    # 這麼多位元組。只影響「怎麼切」，不影響索引結果，所以不納入 index_profile_hash
    index_batch_max_files: int = Field(default=200, ge=1, le=100_000)
    index_batch_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1)

    # --- 搜尋（第二階段切片 8；預設值未實測）---
    # 每個搜尋請求的時間上限（PostgreSQL statement_timeout，內容搜尋另以整體期限限制多批次的總和）
    search_statement_timeout_seconds: float = Field(default=10.0, gt=0, le=600)

    @model_validator(mode="after")
    def _job_settings_are_consistent(self) -> Self:
        if self.job_heartbeat_seconds >= self.job_lease_seconds:
            raise ValueError("JOB_HEARTBEAT_SECONDS must be shorter than JOB_LEASE_SECONDS.")
        if self.job_retry_base_seconds > self.job_retry_max_seconds:
            raise ValueError("JOB_RETRY_BASE_SECONDS must not exceed JOB_RETRY_MAX_SECONDS.")
        # 匯入一次最多做：ensure_repository_root（2 次 Git）、resolve_commit、ls-tree 各一次
        # git_timeout，讀 blob 一次 import_git_timeout；時間上限必須蓋得住這個最壞情況
        worst_case = self.worker_import_git_timeout_seconds + 4 * self.worker_git_timeout_seconds
        if self.worker_task_time_limit_seconds <= worst_case:
            raise ValueError(
                "WORKER_TASK_TIME_LIMIT_SECONDS must exceed WORKER_IMPORT_GIT_TIMEOUT_SECONDS "
                "plus four times WORKER_GIT_TIMEOUT_SECONDS."
            )
        # 註冊 job：GitHub API 一次、clone 一次、其餘本機 Git 呼叫（保守估兩次）
        register_worst_case = (
            self.github_clone_timeout_seconds
            + self.github_api_timeout_seconds
            + 2 * self.worker_git_timeout_seconds
        )
        if self.worker_task_time_limit_seconds <= register_worst_case:
            raise ValueError(
                "WORKER_TASK_TIME_LIMIT_SECONDS must exceed GITHUB_CLONE_TIMEOUT_SECONDS "
                "plus GITHUB_API_TIMEOUT_SECONDS plus twice WORKER_GIT_TIMEOUT_SECONDS."
            )
        if self.managed_temp_orphan_seconds <= self.github_clone_timeout_seconds:
            raise ValueError(
                "MANAGED_TEMP_ORPHAN_SECONDS must exceed GITHUB_CLONE_TIMEOUT_SECONDS."
            )
        return self

    @field_validator("worker_path_mappings", mode="before")
    @classmethod
    def _parse_path_mappings(cls, value: Any) -> Any:
        # 環境變數是 JSON 字串；空白＝沒有映射。NoDecode 讓這裡自己解析
        # （空字串才不會被當成 JSON 錯誤）
        if isinstance(value, str):
            if not value.strip():
                return []
            try:
                return json.loads(value)
            except ValueError:
                raise ValueError("WORKER_PATH_MAPPINGS must be a JSON array.") from None
        return value

    @field_validator("managed_repos_dir", mode="before")
    @classmethod
    def _empty_managed_dir_is_disabled(cls, value: Any) -> Any:
        # .env 的 `MANAGED_REPOS_DIR=` 會是空字串；不處理的話 Path("") 會變成目前目錄
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("managed_repos_dir")
    @classmethod
    def _managed_dir_must_be_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("MANAGED_REPOS_DIR must be an absolute path.")
        return value

    @field_validator("github_clone_base_url")
    @classmethod
    def _clone_base_url_must_be_safe(cls, value: str) -> str:
        # 只允許 https、file，以及指向本機迴圈位址的 http（僅供測試）；
        # 避免 token 經明文 http 送往遠端主機，或讓 clone 使用 ssh、git:// 等其他傳輸
        base = value.strip().rstrip("/")
        parts = urlsplit(base)
        loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
        allowed = (
            parts.scheme in ("https", "file") or (parts.scheme == "http" and loopback)
        ) and not (parts.username or parts.password or parts.query or parts.fragment)
        if not allowed:
            raise ValueError(
                "GITHUB_CLONE_BASE_URL must be an https URL (or file://, or http on loopback)."
            )
        return base

    @field_validator("repository_roots", mode="before")
    @classmethod
    def _split_repository_roots(cls, value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in value.split(os.pathsep) if part.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
