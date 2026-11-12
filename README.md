# RepoPilot

AI 輔助專案客製化平台：管理參考 repos 與目標 repo，依自然語言需求產出附程式碼來源引用的修改方案；後續在獨立工作目錄改碼、執行測試並產出可審查的 diff 與驗證報告。

> 狀態：**第一階段（專案骨架、Repo 管理與固定版本匯入）實作與驗證已完成（附未驗證清單），使用者已於 2026-11-11 確認驗收**。已有：後端 API（本機與 GitHub 來源的 repo 註冊、固定版本匯入、快照與檔案查詢、分支與 commit 歷史、repo 概況、移除）、PostgreSQL＋Alembic，以及最小操作主控台（`frontend/`，無自動化測試）。**未驗證項目**（私有 GitHub repo 與真實 token、大型 repo、Linux／macOS、其他瀏覽器等）見 [docs/roadmap.md](docs/roadmap.md#第一階段整體驗收結論)。第二階段（背景工作、程式結構索引與搜尋）**進行中**：規格已確認；**切片 1～3 已完成**——job 狀態機、Celery＋Redis 的 dispatcher 與 worker 容器、`import-jobs` 背景匯入（見下方「第二階段：背景工作」）；**切片 3b 後端已完成（2026-11-12）**——參考版本（`repository_references`，只是標記、尚未連到 Agent）與移除快照 API（前端未做）；GitHub 背景化、結構索引、搜尋與前端面板整合尚未完成。不宣稱整套平台完成。

## 文件

| 文件 | 內容 |
| --- | --- |
| [docs/learning/stage-01.md](docs/learning/stage-01.md) | **學習筆記**：主控台導覽、介面操作 → API → service → Git／資料表的追蹤表、流程圖、.NET 對照、10 個動手練習 |
| [docs/product.md](docs/product.md) | 產品目標、版本範圍、長期願景 |
| [docs/roadmap.md](docs/roadmap.md) | 九個階段的目標、驗收與目前狀態；第一階段整體驗收結論與驗證紀錄 |
| [docs/architecture.md](docs/architecture.md) | 架構、資料模型、設計決策（D1～D40）、前端主控台結構 |
| [docs/stage-01-slice-5c-spec.md](docs/stage-01-slice-5c-spec.md)、[5d](docs/stage-01-slice-5d-spec.md)、[5e](docs/stage-01-slice-5e-spec.md) | 第一階段切片規格（GitHub 連動、倉庫總覽、版本瀏覽） |
| [docs/stage-02-spec.md](docs/stage-02-spec.md) | 第二階段規格（已確認；切片 1～3 已完成，其餘進行中） |

## 第一階段範圍

- 註冊 `REPOSITORY_ROOTS` 允許目錄內的**本機 Git repo**，或以 `owner/name` 從 **GitHub** 註冊（clone 到受管目錄 `MANAGED_REPOS_DIR`；使用者自行提供 Personal Access Token，建議 fine-grained、Contents 唯讀）。
- 將 revision 解析為完整 commit SHA，**只讀 Git 物件**（不讀工作目錄）保存路徑、UTF-8 文字內容、blob ID、SHA-256、位元組大小與跳過原因；同 repo、同 commit、同匯入設定不重複匯入；`.env`、私鑰與憑證檔不讀內容。
- 查詢 repo、快照、檔案、分支與標籤、commit 歷史與 repo 概況；GitHub 來源可 fetch；可移除已註冊的 repo。
- 操作主控台：repo 卡片總覽、版本瀏覽（分支下拉＋ commit 圖）、檔案樹、GitHub 連線。

不在第一階段：LLM、向量搜尋、改碼、GitLab 與其他遠端來源、背景 worker、多使用者登入。

## 環境需求

- Python 3.12（以 uv 管理：`uv python install 3.12`）與 [uv](https://docs.astral.sh/uv/)
- Docker Desktop（執行 PostgreSQL）
- Git CLI
- Node.js 與 npm（只有操作主控台需要；以 Node 24.11.1、npm 11.6.2 實測）

## 快速開始

以下在專案根目錄的 Windows PowerShell 執行。步驟已於切片 2～5b（2026-10-24～2026-11-09）逐步驗證；2026-11-10 另實跑 `docker compose ps`、`uv run alembic current`、`uv run pytest tests/unit -q` 與前端 `npm run typecheck`／`lint`（見 [roadmap](docs/roadmap.md#第一階段整體驗收結論)）。

```powershell
# 1. 安裝後端依賴、啟動資料庫（db：開發，5432；db-test：整合測試，5433，資料不保存）
uv sync
docker compose up -d --wait db db-test

# 2. 建立 .env（見下一節；至少設定 REPOSITORY_ROOTS，要用 GitHub 功能再設 TOKEN_ENCRYPTION_KEY 與 MANAGED_REPOS_DIR）
#    已經有 .env 時不要再執行這行（Copy-Item 會直接覆蓋）
Copy-Item .env.example .env

# 3. 建立資料表（目前最新 migration 為 0007；0006 為第二階段切片 2 的 job 資料表、0007 為切片 3b 的參考版本，升級既有資料庫請自行執行）
uv run alembic upgrade head
uv run alembic current        # 預期 0007 (head)

# 4. 啟動後端（終端機 1）
uv run uvicorn repopilot.main:create_app --factory --reload
```

```powershell
# 5. 啟動主控台（終端機 2）；後端必須先啟動
cd frontend
npm install
npm run dev                   # 以終端機顯示的網址為準；預設 http://localhost:5173，被占用時 Vite 會改埠
```

驗證：

```powershell
curl.exe -i http://127.0.0.1:8000/health   # 程序存活，不檢查資料庫
curl.exe -i http://127.0.0.1:8000/ready    # 驗證資料庫；無法連線回 503 database_unavailable
```

Swagger UI：`http://127.0.0.1:8000/docs`。第一次使用：打開主控台 →「新增 repo」→ 輸入後端主機上的絕對路徑（須在 `REPOSITORY_ROOTS` 內）→ 回到首頁看卡片（尚未分析的卡片會自動匯入預設分支最新 commit）→「查看詳情」選分支與 commit。

## 設定（`.env`）

複製 `.env.example` 為 `.env` 後修改；**不要提交 `.env`**（已在 `.gitignore`）。`DATABASE_URL` 的預設值對應 `docker-compose.yml` 的 `db`（本機開發用假帳密，請勿用於共用或正式環境）。`.env` 範例（假值）：

```text
REPOSITORY_ROOTS=D:\repos
MANAGED_REPOS_DIR=D:\repopilot-managed
TOKEN_ENCRYPTION_KEY=<由下方指令產生的金鑰>
```

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `APP_NAME`、`LOG_LEVEL` | `RepoPilot`、`INFO` | 應用程式名稱；日誌等級（單行 JSON，帶 `request_id`） |
| `DATABASE_URL` | 見 `.env.example` | PostgreSQL 連線字串（psycopg 3，`postgresql+psycopg://…`） |
| `DB_CONNECT_TIMEOUT_SECONDS`、`DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT_SECONDS` | 5、5、10、30 | 連線逾時與連線池（預設同 SQLAlchemy，未經實測；池滿等待逾時時請求會失敗） |
| `REPOSITORY_ROOTS` | 空 | 允許註冊的**本機 repo** 根目錄，多個以 `;` 分隔；**空白＝拒絕所有本機註冊** |
| `GIT_TIMEOUT_SECONDS` | 30 | 單一 Git 指令逾時 |
| `IMPORT_MAX_FILE_BYTES`、`IMPORT_MAX_FILES`、`IMPORT_MAX_SNAPSHOT_BYTES`、`IMPORT_GIT_TIMEOUT_SECONDS`、`IMPORT_STALE_SECONDS` | 1 MiB、20,000、200 MiB、300、1800 | 匯入上限與「`importing` 逾時可被接手」秒數；數值未經實測 |
| `IMPORT_EXCLUDED_PATTERNS`、`IMPORT_EXCLUDED_EXCEPTIONS` | 內建秘密檔規則 | JSON 陣列，比對檔名（不分大小寫）；會取代預設值並納入 `import_profile_hash` |
| `TOKEN_ENCRYPTION_KEY` | 空 | GitHub token 的 Fernet 加密金鑰；**空白時無法保存 token，沒有預設金鑰**；金鑰遺失或更換後已保存的 token 無法解密，需重新提交 |
| `GITHUB_API_BASE_URL`、`GITHUB_CLONE_BASE_URL` | `https://api.github.com`、`https://github.com` | 僅供測試指向本機假 GitHub |
| `GITHUB_API_TIMEOUT_SECONDS`、`GITHUB_CLONE_TIMEOUT_SECONDS` | 10、300 | 呼叫 GitHub API 與 clone／fetch 的逾時（未實測，大型 repo 可能需調高） |
| `MANAGED_REPOS_DIR` | 空 | GitHub clone 的**受管目錄**（絕對路徑）；**空白＝停用 GitHub 註冊**；與 `REPOSITORY_ROOTS` 分開 |
| `TEST_DATABASE_URL` | `db-test` 預設值 | 只供整合測試讀取環境變數（不讀 `.env`）；資料庫名稱必須含 `test` |
| `WORKER_PATH_MAPPINGS` | 空 | 第二階段：主機路徑 ↔ 容器路徑（JSON 陣列 `[{host, container, mode}]`）；API 建立 import-job 時用它檢查路徑可映射，未涵蓋回 503 `worker_path_not_mapped`；容器的設定由 docker-compose 提供 |
| `WORKER_GIT_TIMEOUT_SECONDS`、`WORKER_IMPORT_GIT_TIMEOUT_SECONDS`、`WORKER_TASK_TIME_LIMIT_SECONDS` | 120、900、3600 | worker 的 Git 逾時（R1）與任務時間上限（須大於 900＋4×120）；未實測，只影響 worker |
| `CELERY_BROKER_URL`、`CELERY_QUEUE_NAME`、`CELERY_GLOBAL_KEYPREFIX` | 空、`repopilot.jobs`、`repopilot:` | 只有 dispatcher 與 worker 使用（docker-compose 已設定）；**API 不讀、不連 Redis** |
| `WORKER_REPOSITORIES_HOST_DIR`、`WORKER_MANAGED_HOST_DIR` | 專案內 `.test-repos`、`.test-managed` | 只給 docker compose 插值（後端不讀）：worker 容器掛載的主機目錄，**用正斜線** |
| `JOB_*`、`DISPATCH_*`、`RECOVERY_SCAN_SECONDS`、`SERVICE_HEARTBEAT_SECONDS` | 見 `.env.example` | 租約、重試、派送與心跳（切片 2、3；皆未實測的起點） |

產生 `TOKEN_ENCRYPTION_KEY`（輸出貼到 `.env`；**不要把真實金鑰貼進文件或版控**）：

```powershell
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

### 路徑說明

- **本機 repo 路徑**：要註冊的 Git repo（例如 `D:\repos\demo`）必須位於 `REPOSITORY_ROOTS` 內，由**後端所在的主機**直接讀取；介面輸入的是後端主機上的路徑，不是瀏覽器所在機器的路徑。`MANAGED_REPOS_DIR` 內的路徑不能以本機來源註冊。
- **受管目錄** `MANAGED_REPOS_DIR`：只存放系統自己 clone 的 GitHub repo（`github\<owner>\<name>`，clone 進行中的暫存目錄在 `.tmp\`），**不要**放進 `REPOSITORY_ROOTS`，也不要指向既有的重要目錄；第一次註冊時才建立。
- **容器 volume**：只保存 PostgreSQL 資料庫檔，**不是**被匯入的 Git repo 位置。

## 測試與品質檢查

```powershell
uv run pytest                      # 完整套件
uv run pytest -m "not slow"        # 略過啟動真實 uvicorn 子程序的重啟持久化測試
uv run pytest tests/unit -q        # 只跑單元測試（不需要資料庫）
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
uv run alembic check               # model 與 migration 無差異（以 DATABASE_URL 指向的資料庫為準）
```

- 整合測試使用真實 PostgreSQL（`db-test` 容器，埠 5433，資料不保存）；**`db-test` 沒啟動時整合測試會明確失敗，不會被跳過**。`pytest` 會重建測試資料庫的 `public` schema，且只允許資料庫名稱含 `test`，不會碰 dev 資料庫。
- **`stack` 測試**（第二階段切片 3，`tests/integration/stack/`）需要真實的 Redis、dispatcher 與 worker 容器：預設**會執行**（`uv run pytest` 包含它們）；前置是 `docker compose up -d --wait db-test` 與 `docker compose --profile jobs build`。測試會自己啟動 `redis-test`、`dispatcher-test`、`worker-test`（只在這些測試期間）並在結束後停止；Docker 不可用或映像沒建置時**明確失敗，不跳過**。只想跑不需要容器的測試：`uv run pytest -m "not stack"`。整合測試開始時會先停止仍在執行的 `worker-test`／`dispatcher-test`（它們會去處理測試用的假 job）。
- `tests/integration/test_restart_persistence.py` 會在 `db-test` 容器內建立臨時資料庫並啟動真實 uvicorn 子程序（空閒埠，不碰 8000 埠；結束即 DROP 與停止），以 `slow` marker 標記，預設仍會執行。
- 完整套件在 Windows 開發機上約 13～20 分鐘（2026-11-10 實測：811 秒、911 秒、1059 秒；2026-11-11 第二階段切片 2 後：859 秒；切片 3 含 `stack` 測試：以 [roadmap](docs/roadmap.md) 的最近驗證為準）。最近一次完整結果見 [roadmap「最近驗證（2026-11-11，第二階段切片 3）」](docs/roadmap.md)；前序紀錄見 [roadmap](docs/roadmap.md#最近驗證2026-11-10切片-6)。
- 前端沒有自動化測試，見下方「操作主控台」。

## 第二階段：背景工作（開發中；切片 1～3 已完成）

**目前能做什麼（切片 3，2026-11-11）**：`POST /api/v1/repositories/{id}/import-jobs` 立即回 202 與 job，由 dispatcher（outbox→Redis）與 worker（Celery，Docker Linux 容器）在背景匯入**提交當下解析出的完整 commit SHA**；以 `GET /api/v1/jobs/{id}` 輪詢進度。第一階段的同步 `POST …/imports` **保留並標示 deprecated**（長時間匯入請改用 `import-jobs`）。**沒有**：GitHub 註冊與 fetch 的背景版本（切片 5）、索引與搜尋（切片 6～8）、取消／重試的前端按鈕與完整行為驗證（切片 4）、前端的「匯入此版本」與進度面板（前端另行處理）。API 程序**不連 Redis**；Redis 與 worker 都停止時 API 仍接受 job（維持 `queued`，`GET /api/v1/jobs/status` 顯示沒有 worker／dispatcher）。設計與取捨見 [architecture.md D39、D40](docs/architecture.md)。

**路徑與掛載**：worker 在 Linux 容器內，看不到 Windows 路徑，所以以 bind mount 掛入並以 `WORKER_PATH_MAPPINGS` 映射：`REPOSITORY_ROOTS` 以**唯讀**掛載、`MANAGED_REPOS_DIR` 以讀寫掛載。API 建立 job 時會檢查 repo 路徑能映射到容器，否則回 503 `worker_path_not_mapped`——所以 **API 的 `.env` 也要設定 `WORKER_PATH_MAPPINGS`**。

```powershell
# 1. 建置映像（worker 與 dispatcher 共用；依 uv.lock 安裝依賴，首次約 1～2 分鐘）
docker compose --profile jobs build

# 2. 在 .env 加入（主機路徑，**用正斜線**；必須分別涵蓋 REPOSITORY_ROOTS 與 MANAGED_REPOS_DIR）
#    WORKER_REPOSITORIES_HOST_DIR=D:/repos
#    WORKER_MANAGED_HOST_DIR=D:/repopilot-managed
#    WORKER_PATH_MAPPINGS=[{"host":"D:/repos","container":"/mnt/repos","mode":"ro"},{"host":"D:/repopilot-managed","container":"/mnt/managed","mode":"rw"}]
#    （前兩個只給 docker compose 用；WORKER_PATH_MAPPINGS 給 API 檢查用）。
#    選用：複製 .env.worker.example 為 .env.worker（不納入版控）放 worker 專用設定，例如 TOKEN_ENCRYPTION_KEY（切片 5 才會用到）。

# 3. 啟動：資料庫、Redis、dispatcher、worker（開發堆疊，連開發的 db；profile jobs）
docker compose --profile jobs up -d --wait db redis dispatcher worker

# 4. 重啟 API 讓它讀到 WORKER_PATH_MAPPINGS，再確認 dispatcher 與 worker 在線
Invoke-RestMethod http://127.0.0.1:8000/api/v1/jobs/status    # dispatcher.status、worker.status 應為 online

# 5. 停止（只停止；不要用 docker compose down，那會連 db 一起處理）
docker compose --profile jobs stop worker dispatcher redis
```

**驗證狀態**：上述啟動流程與 import-job 流程（註冊→`import-jobs`→輪詢→查詢快照檔案；再以 `docker compose kill worker` 殺掉執行中的 worker，由復原掃描重排並成功）已於 2026-11-11 以**拋棄式資料庫 `repopilot_smoke_s3`**（建立在 `db` 容器內，結束後已 DROP；**沒有碰開發資料庫 `repopilot`**）與臨時 API（埠 8011）實跑通過；本節的 PowerShell 文字與實跑的指令相近但**沒有逐字複製執行**。`up -d --wait` 靠容器 healthcheck（心跳是否新鮮）判斷就緒。開發堆疊連開發 `db` 的**真實資料庫流程未實跑**（避免寫入你的 jobs 與快照）。

**測試堆疊（自動化測試用，R5 隔離）**：`redis-test`、`dispatcher-test`、`worker-test`（profile `jobs-test`）連 `db-test`，與開發的 `db`、`redis` **分屬不同 Docker 網路**（互相解析不到對方的名稱；2026-11-11 以容器內實測）。它們由 `stack` 測試自動啟停；手動操作時需先設定 `WORKER_TEST_REPOS_HOST_DIR`、`WORKER_TEST_MANAGED_HOST_DIR` 為專案內 `.test-repos`、`.test-managed` 的絕對路徑（正斜線），見 `tests/stack_control.py`。

**R1（掛載上讀取 loose 物件很慢）**：容器讀 Windows 掛載比主機慢，從未 `git gc` 且 loose 物件極多的本機 repo 可能超過逾時（切片 1 單次實測：20,000 個 loose 物件約 391 秒）。worker 的 Git 逾時可設定（`WORKER_GIT_TIMEOUT_SECONDS`、`WORKER_IMPORT_GIT_TIMEOUT_SECONDS`，預設 120／900，未實測）；逾時時 job 的錯誤訊息會提示可在該 repo 自行執行 `git gc`（RepoPilot 不會替你的 repo 執行 gc）。

**job 狀態機與 `GET /jobs/*`（切片 2）**：已新增 migration `0006`（`jobs`、`job_dispatches`、`service_heartbeats`）與 5 個 job 端點（見下方 API 一覽）。設定（租約秒數等）見 `.env.example`，皆為未實測的預設值。

spike 的實測腳本與結果見 `scripts\spike\`（腳本檔頭有說明）與 `docs\roadmap.md`（2026-11-11，部分通過）；切片 3 起 `worker-test` 不再是空轉容器，spike 腳本中依賴它的部分已不適用（腳本未修改也未重跑）。

## API 一覽

共 29 個端點（可用 `http://127.0.0.1:8000/openapi.json` 對照；契約與取捨見 [architecture.md](docs/architecture.md#設計決策) 的 D 編號）。其中 5 個 job 端點為第二階段切片 2 新增、`import-jobs` 為切片 3 新增、參考版本與移除快照的 4 個端點為切片 3b 新增。

| 方法 | 路徑 | 說明 | 主要狀態碼與錯誤碼 |
| --- | --- | --- | --- |
| GET | `/health` | 程序存活，不檢查資料庫 | 200 |
| GET | `/ready` | 驗證資料庫連線 | 200；503 `database_unavailable` |
| POST | `/api/v1/repositories` | 註冊本機 repo（body `{"path": "...", "name": "..."}`，`name` 可省略） | 201 新建；200 已註冊（路徑不同寫法會去重）；422 `invalid_repository_path` |
| GET | `/api/v1/repositories` | repo 列表 `{items}`（含 `source_type`、`github_*` 欄位） | 200 |
| GET | `/api/v1/repositories/{id}` | 單一 repo | 200；404 `repository_not_found`；422 `validation_error`（id 超出 1～2³¹−1） |
| DELETE | `/api/v1/repositories/{id}` | 移除 repo 與其快照、檔案；**本機來源只刪紀錄、絕不碰磁碟**；GitHub 來源另刪受管目錄內的 clone | 200 `{repository_id, source_type, deleted_snapshots, deleted_files, removed_clone}`；404；409 `repository_busy`；422 `invalid_repository_path`；500 `managed_storage_error`；503 `github_registration_disabled`（[D33](docs/architecture.md#d33-實作進度移除已註冊的-repo2026-11-10)） |
| POST | `/api/v1/repositories/github` | 以 `owner/name`（或 `https://github.com/owner/name`）註冊並 clone（body `{"repo": "..."}`） | 201；200 已註冊（不重新 clone）；422 `invalid_github_repo`、`github_token_invalid`；404 `github_repo_not_found_or_private`；503 `github_registration_disabled`；502 `github_clone_failed`、`github_unreachable`、`github_api_error`；504 `github_clone_timeout`、`github_timeout`；500 `managed_storage_error`（[D32](docs/architecture.md#設計決策)） |
| POST | `/api/v1/repositories/{id}/fetch` | 僅 GitHub 來源：`git fetch --prune origin`，回報 ref 變動；不建立快照 | 200 `{repository_id, updated_refs, truncated}`；409 `repository_not_github`；502 `github_fetch_failed`；504 `github_fetch_timeout`；422 `invalid_repository_path` |
| POST | `/api/v1/repositories/{id}/imports` | **【已棄用 deprecated】**同步匯入固定版本快照（body `{"revision": "main"}`，可省略＝`HEAD`；分支、tag、SHA 皆可）；請求會占用到匯入結束，長時間匯入請改用 `import-jobs`；與 worker 共用快照鎖 | 201 新建；200 相同快照已完成；409 `snapshot_import_in_progress`；422 `invalid_revision`、`invalid_repository_path`、`import_limit_exceeded`；504 `git_timeout` |
| POST | `/api/v1/repositories/{id}/import-jobs` | **背景匯入**（第二階段切片 3）：body 同上；API 先在主機端把 revision 解析成完整 SHA（無效回 422、不建立 job）並檢查路徑可映射到 worker 容器，再同一交易建立 job 與待派送事件；以 `GET /jobs/{id}` 輪詢 | 202 `{job, snapshot_id, repository_id, reused}`＋`Location`（新建、重新排入失敗或取消的同一個 job、或回既有進行中的 job）；200 `reused=true`（相同快照已完成，`job` 為產生它的最近一次成功 job，同步匯入的快照則為 `null`）；404；409 `snapshot_import_in_progress`；422 `invalid_revision`、`invalid_repository_path`；503 `worker_path_not_mapped`（[D40](docs/architecture.md)） |
| GET | `/api/v1/repositories/{id}/snapshots` | 快照列表（新的在前；含 `importing`／`failed`；`?status=`、`limit`、`offset`）；每筆另有 `is_referenced`、`is_stale_profile`（匯入設定與目前不同；切片 3b 增量） | 200 `{items, total, limit, offset}` |
| GET | `/api/v1/repositories/{id}/refs` | 分支與標籤（唯讀 Git；本機來源列 `refs/heads/*`，GitHub 來源列 `origin/*`） | 200 `{default_ref, head_sha, branches, tags, truncated}`；404；422；504 `git_timeout` |
| GET | `/api/v1/repositories/{id}/commits` | commit 歷史（`--topo-order`；`ref`、`limit` 預設 100 最大 500、`offset`）；`ref` 須是 `/refs` 回傳的完整 ref | 200 `{items, total_returned, has_more}`；422 `invalid_revision`、`validation_error`（[D34](docs/architecture.md#d34-實作進度切片-5e-1refs-與-commits-後端-api2026-11-10)） |
| GET | `/api/v1/repositories/{id}/overview` | 預設分支最新 commit 的概況：統計、語言分布、README 前 4,000 字元、專案資訊；**唯讀、不匯入** | 200（`analyzed=false` 時只有 `head`）；404；422；504（[D35](docs/architecture.md#d35-實作進度切片-5d-1repo-概況後端-api2026-11-10)） |
| GET | `/api/v1/snapshots/{id}` | 快照詳情與 `file_status_counts`、`skip_reason_counts`（只有 `completed` 才統計）；另有 `is_referenced`、`is_stale_profile`（切片 3b 增量） | 200；404 `snapshot_not_found` |
| DELETE | `/api/v1/snapshots/{id}?force=false` | **移除快照**（切片 3b）：只刪資料庫紀錄（快照與檔案），**不動 Git repo**；匯入中、進行中的 job、快照鎖被持有 → `snapshot_busy`（`force` 無法繞過，什麼都不刪）；是參考版本且未帶 `force=true` → `snapshot_referenced`；移除後同一 commit 可重新匯入 | 200 `{snapshot_id, repository_id, deleted_files, was_referenced}`；404 `snapshot_not_found`；409 `snapshot_busy`、`snapshot_referenced`；422（[D41](docs/architecture.md)） |
| GET | `/api/v1/references` | 所有 repo 的**參考版本**（只讀資料庫；只是標記，**尚未連到 Agent**） | 200 `{items: [{id, repository, snapshot_id, commit_sha, snapshot_status, file_count, skipped_count, total_bytes, created_at}]}` |
| PUT | `/api/v1/repositories/{id}/reference` | 把 repo 的某個 `completed` 快照設為參考版本（body `{"snapshot_id": N}`；每個 repo 最多一個，重複設定冪等） | 200 參考；404 `repository_not_found`、`snapshot_not_found`（含別的 repo 的快照）；409 `snapshot_not_completed`；422 |
| DELETE | `/api/v1/repositories/{id}/reference` | 取消參考版本（冪等） | 204；404 `repository_not_found`；422 |
| GET | `/api/v1/snapshots/{id}/files` | 檔案列表（不含 `content`；`status`、`path_prefix`、`limit`、`offset`） | 200 `{items, total, limit, offset}`；409 `snapshot_not_completed` |
| GET | `/api/v1/snapshots/{id}/files/{file_id}` | 單檔含 `content`（`skipped` 為 `null`，帶 `skip_reason`） | 200；404 `snapshot_file_not_found`；409 `snapshot_not_completed`（[D30](docs/architecture.md#設計決策)） |
| PUT | `/api/v1/github/token` | 驗證（呼叫 GitHub `GET /user`）後加密保存 token（body `{"token": "..."}`） | 200 `{authenticated, login, token_type, warning}`；422 `validation_error`、`github_token_invalid`；503 `token_encryption_not_configured`；502／504 |
| GET | `/api/v1/github/auth` | 只回狀態，**永不回傳 token**；不呼叫 GitHub | 200 |
| DELETE | `/api/v1/github/auth` | 刪除本機保存的 token（不在 GitHub 端撤銷） | 204（冪等） |
| GET | `/api/v1/jobs` | 背景工作列表（新的在前；`status`＝`queued`／`running`／`succeeded`／`failed`／`cancelled`／`active`、`kind`、`repository_id`、`snapshot_id`、`limit`、`offset`） | 200 `{items, total, limit, offset}`；422 `validation_error` |
| GET | `/api/v1/jobs/status` | dispatcher／worker 是否在線（讀 `service_heartbeats`，**不連 Redis**）與 `queued`／`running` 數量；`status` 為 `online`／`stale`／`absent` | 200 |
| GET | `/api/v1/jobs/{job_id}` | 單一工作（階段、進度、嘗試次數、安全的 `error`、`result`；**不含租約令牌**） | 200；404 `job_not_found`；422 `validation_error`（id 超出 1～2⁶³−1） |
| POST | `/api/v1/jobs/{job_id}/cancel` | 取消：`queued` 立即 `cancelled`；`running` 只記錄取消請求；已取消者冪等 | 200 已取消；202 已記錄請求（`running`）；404；409 `job_not_cancellable` |
| POST | `/api/v1/jobs/{job_id}/retry` | 重試 `failed` 的工作（同一 job id，重新排入佇列） | 202；404；409 `job_not_retryable` |

通則：

- 錯誤格式一律為 `{"error": {"code": "...", "message": "...", "request_id": "..."}}`（422 另有 `details`，不回顯輸入）；每個回應帶 `X-Request-ID`（請求可自帶 1～64 個英數、`.`、`_`、`-`，不合法時由伺服器重新產生）。
- 列表分頁參數 `limit`（預設 50、最大 200）與 `offset`（預設 0），超出範圍回 422，不靜默截斷。
- 路徑中的 id 限制在資料庫欄位範圍，超出回 422。
- 匯入、clone、fetch 與移除預設仍是**同步請求**；第二階段切片 3 起匯入另有背景版本 `import-jobs`（GitHub 註冊與 fetch 的背景版本屬切片 5）。job 端點只查詢與操作 job 狀態，不執行任何工作（工作由 worker 執行）。

### PowerShell 範例

**驗證狀態**：下列 **GET** 範例（列表、`/refs`、`/commits`、`/overview`、快照與檔案查詢、`/github/auth`）已於 2026-11-10 對使用者已在執行的後端實跑（只讀）。**POST／PUT／DELETE 範例的文字沒有逐字實跑（未驗證）**；對應的流程已由自動化測試與 2026-11-10 的手動煙霧測試驗證（[roadmap](docs/roadmap.md)），GitHub 私有 repo 與真實 token 流程**未驗證**。**背景匯入與 `/jobs` 範例**的文字也沒有逐字實跑；對應的流程已於 2026-11-11 以拋棄式資料庫、臨時 API（8011 埠）與容器實跑（見上方「第二階段：背景工作」的驗證狀態），回應形狀以實際回應為準。路徑與 id 都是假的（`D:\repos\demo`）。**切片 3b 的參考版本與移除快照範例**：2026-11-12 以臨時 API（8013 埠）與拋棄式資料庫 `repopilot_smoke_3b`（已 DROP，**未碰開發資料庫**）實跑過同樣的流程（`Invoke-WebRequest`／`Invoke-RestMethod`，路徑為臨時目錄），**但 README 這一段的文字沒有逐字複製執行**；`snapshot_busy` 只有自動化測試，沒有手動實跑；前端未做。

```powershell
$api = "http://127.0.0.1:8000/api/v1"

# 註冊本機 repo（路徑須在 REPOSITORY_ROOTS 內）→ 匯入 main 的最新 commit；重複匯入回 200（同一個快照）
$repo = Invoke-RestMethod -Method Post -Uri "$api/repositories" -ContentType "application/json" `
  -Body (@{ path = "D:\repos\demo" } | ConvertTo-Json)
$snap = Invoke-RestMethod -Method Post -Uri "$api/repositories/$($repo.id)/imports" `
  -ContentType "application/json" -Body '{"revision": "main"}'

# 查詢快照與檔案（GET；不含 content，單檔才有）
Invoke-RestMethod "$api/snapshots/$($snap.id)"                      # file_status_counts、skip_reason_counts
$page = Invoke-RestMethod "$api/snapshots/$($snap.id)/files?path_prefix=src/&limit=20"
$page.items | Select-Object id, path, status
Invoke-RestMethod "$api/snapshots/$($snap.id)/files/$($page.items[0].id)"

# 分支、commit 與概況（GET，唯讀）
$refs = Invoke-RestMethod "$api/repositories/$($repo.id)/refs"
$ref = [uri]::EscapeDataString($refs.default_ref)                   # ref 須是 /refs 回傳的完整名稱
(Invoke-RestMethod "$api/repositories/$($repo.id)/commits?ref=$ref&limit=20").items | Select-Object sha, subject, imported
$o = Invoke-RestMethod "$api/repositories/$($repo.id)/overview"     # analyzed=false 時請先 POST /imports
```

```powershell
# GitHub：貼 token（用 Read-Host 避免進入命令歷史；真實 token 不要貼進文件或腳本）
$secure = Read-Host "GitHub token" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $secure).Password
Invoke-RestMethod -Method Put -Uri "$api/github/token" -ContentType "application/json" -Body (@{ token = $plain } | ConvertTo-Json)
Remove-Variable plain, secure
Invoke-RestMethod "$api/github/auth"                                # 只回狀態

# GitHub：註冊公開 repo（不需要 token）→ 匯入 → fetch；fetch 之後用 origin/<分支> 匯入新 commit
$gh = Invoke-RestMethod -Method Post -Uri "$api/repositories/github" -ContentType "application/json" `
  -Body (@{ repo = "octocat/Hello-World" } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$api/repositories/$($gh.id)/fetch"
Invoke-RestMethod -Method Post -Uri "$api/repositories/$($gh.id)/imports" -ContentType "application/json" -Body '{"revision": "origin/master"}'

# 移除（會刪除快照；GitHub 來源另刪本機 clone；本機來源不碰磁碟）
Invoke-RestMethod -Method Delete -Uri "$api/repositories/$($gh.id)"
```

```powershell
# 背景匯入（第二階段切片 3；需先依「第二階段：背景工作」啟動 dispatcher 與 worker，並在 .env 設定 WORKER_PATH_MAPPINGS）
$job = Invoke-RestMethod -Method Post -Uri "$api/repositories/$($repo.id)/import-jobs" `
  -ContentType "application/json" -Body '{"revision": "main"}'      # 202：立即回 job（status=queued）；快照已完成時 200、reused=true
do {
  Start-Sleep -Seconds 1
  $j = Invoke-RestMethod "$api/jobs/$($job.job.id)"                 # status：queued → running → succeeded／failed／cancelled
  "{0} {1} {2}/{3}" -f $j.status, $j.phase, $j.progress.processed_files, $j.progress.total_files
} while ($j.status -in "queued", "running")
Invoke-RestMethod "$api/snapshots/$($j.snapshot_id)/files?limit=20" # succeeded 後 job.snapshot_id 有值

# 背景工作的查詢與操作（切片 2）
Invoke-RestMethod "$api/jobs/status"                                # dispatcher／worker：online／stale／absent（讀心跳，不連 Redis）
Invoke-RestMethod "$api/jobs?status=active&limit=20"                # 進行中的工作（新的在前）
Invoke-RestMethod "$api/jobs/1"                                     # 單一工作；不存在回 404 job_not_found
Invoke-RestMethod -Method Post -Uri "$api/jobs/1/cancel"            # queued 立即取消（200）；running 只記錄請求（202）
Invoke-RestMethod -Method Post -Uri "$api/jobs/1/retry"             # 只有 failed 可重試（202）；否則 409 job_not_retryable
```

```powershell
# 參考版本與移除快照（第二階段切片 3b；參考只是標記，尚未連到 Agent）。需要 repo 已有 completed 快照（上面的 $snap）
Invoke-RestMethod -Method Put -Uri "$api/repositories/$($repo.id)/reference" -ContentType "application/json" `
  -Body (@{ snapshot_id = $snap.id } | ConvertTo-Json)               # 200；快照須屬於該 repo 且 completed；重複設定冪等
(Invoke-RestMethod "$api/references").items | Select-Object id, snapshot_id, commit_sha, @{ n = "repo"; e = { $_.repository.name } }
(Invoke-RestMethod "$api/repositories/$($repo.id)/snapshots").items | Select-Object id, status, is_referenced, is_stale_profile

# 移除快照：只刪資料庫紀錄，不動 Git repo。它是參考版本時 409 snapshot_referenced，帶 force=true 才會一併取消參考並移除
try { Invoke-RestMethod -Method Delete -Uri "$api/snapshots/$($snap.id)" } catch { $_.ErrorDetails.Message }
Invoke-RestMethod -Method Delete -Uri "$api/snapshots/$($snap.id)?force=true"   # 200 {snapshot_id, repository_id, deleted_files, was_referenced}
Invoke-RestMethod -Method Delete -Uri "$api/repositories/$($repo.id)/reference"  # 取消參考；本來就沒有也成功（204，冪等）
```

注意：

- **fetch 之後請用 `origin/<分支>` 匯入新 commit**：clone 時建立的本機 `HEAD` 不會被 fetch 移動；主控台的版本區會自動使用 `/refs` 列出的名稱。
- clone 是完整 clone（非 shallow）、`--no-checkout`（受管目錄內只有 `.git`）；大型 repo 佔用時間與磁碟，逾時由 `GITHUB_CLONE_TIMEOUT_SECONDS` 限制，過大 repo 的行為未實測。
- `owner`／`name` 的驗證比 GitHub 實際允許的更窄：名稱不得以 `-` 開頭、不得以 `.` 結尾（以 `.` 開頭可以，例如 `.github`），也不接受 Windows 保留名稱（`con`、`nul`、`com1` 等）。
- Windows PowerShell 5.1 的 `Invoke-RestMethod` 可能把 UTF-8 中文顯示成亂碼（回應沒有 charset），只是顯示問題；用 `curl.exe` 或 PowerShell 7 可正常顯示。
- 多個 worker 程序同時註冊同一個 GitHub repo 時，行程內的鎖不共用，輸家可能收到 500 `managed_storage_error`，重試即回 200。

### 自行驗證私有 GitHub repo（**未驗證**，需要你自己的 token）

私有 repo 與真實 token 的 clone／fetch 目前沒有驗證紀錄（沒有真實 token 可測）。步驟（token 只在你的機器上輸入）：

```powershell
# 1. 在 GitHub 建立 fine-grained PAT：只選要用的 repo，Permissions 只給 Contents: Read-only
#    .env 設定 TOKEN_ENCRYPTION_KEY 與 MANAGED_REPOS_DIR，重啟後端，然後貼 token（見上方 PUT /github/token 範例，或用主控台「GitHub 連線」）

# 2. 註冊你的私有 repo
$repo = Invoke-RestMethod -Method Post -Uri "$api/repositories/github" -ContentType "application/json" `
  -Body (@{ repo = "<你的帳號>/<私有 repo>" } | ConvertTo-Json)
$repo.github_is_private      # 預期 True

# 3. 確認 token 沒有落盤：兩個指令都不應有輸出（只找標頭設定，不會印出 token）
git -C $repo.canonical_local_path config --list | Select-String "extraheader|x-access-token"
Select-String -Path "$($repo.canonical_local_path)\.git\config" -Pattern "extraheader|x-access-token|Authorization"

# 4. 在 GitHub 推一個新 commit 後 fetch，再用 origin/<分支> 匯入
Invoke-RestMethod -Method Post -Uri "$api/repositories/$($repo.id)/fetch"
```

若註冊回 404 `github_repo_not_found_or_private`，代表 token 看不到該 repo（未涵蓋、權限不足，或組織要求核准）；回 422 `github_token_invalid` 代表 token 無效或已過期，請重新提交。

## 操作主控台

React + TypeScript + Vite 的最小操作主控台（`frontend/`），用來以點擊理解「註冊 → 總覽 → 版本 → 檔案」的流程，並對照 API；只呼叫上列既有 API，不在前端重做業務規則，也沒有登入。結構見 [architecture.md「前端主控台結構」](docs/architecture.md#前端主控台結構)，逐區塊導覽見[學習筆記](docs/learning/stage-01.md#12-主控台導覽)。

畫面：頁首後端狀態；「GitHub 連線」與「新增 repo」摺疊區；倉庫總覽（repo 卡片、參考清單、GitHub 來源的更新與兩段式確認的移除）；repo 詳情的版本區（分支下拉＋ SVG commit 圖）、預設收合的檔案樹、進階快照列表。

| 指令（在 `frontend/` 執行） | 說明 |
| --- | --- |
| `npm run dev` | 開發伺服器；把 `/api`、`/health`、`/ready` 轉發到 `http://127.0.0.1:8000`（`frontend/vite.config.ts`），所以後端**不需要、也沒有開放 CORS** |
| `npm run typecheck` | `tsc --noEmit`（app 與 vite 設定兩份 tsconfig） |
| `npm run lint` | oxlint（`create-vite` 範本預設，不是 ESLint） |
| `npm run build` | `tsc -b` 型別檢查後建置到 `frontend/dist/`（不會被提交）；建置結果沒有 proxy，需另行提供同源的 `/api`，**未驗證** |

驗證狀態：`typecheck`、`lint` 於 2026-11-10 實跑通過（exit 0）；各切片的瀏覽器操作驗證（5b 無頭 Edge；5c-3、5d-2、5e-2 Chromium／Edge）與其範圍見 [roadmap](docs/roadmap.md)。**前端沒有自動化測試**；其他瀏覽器、窄螢幕與觸控、鍵盤與螢幕閱讀器完整操作、超大歷史與大量 repo 的效能**未驗證**。

## 安全注意事項

- **GitHub token**：只加密保存於資料庫（Fernet），不進命令列、日誌、回應、前端儲存與 `.git/config`；API 永不回傳它。token 會從瀏覽器經 HTTP 送到後端，**僅限本機 `127.0.0.1` 使用，不得在未加 TLS 的遠端部署使用**；目前沒有登入與資源隔離，不要對外網開放。
- **私有 repo 的內容會存進本機資料庫**（快照內容）；請知悉資料庫保存了私有程式碼，並妥善保護資料庫與備份。
- 匯入預設排除 `.env`、私鑰與憑證檔（只記路徑與原因，不讀內容）；repo 內容視為不可信資料。請勿將真實金鑰放入程式、測試或文件，也不要提交 `.env`。
- 預設資料庫帳密（`docker-compose.yml`）是本機開發用假值，不可用於共用或正式環境。
- 系統不自動 commit、push、建立 PR 或合併；匯入與瀏覽只讀 Git 物件與 refs，不修改你的本機 repo。

## 長期願景

希望最終能透過系統介面輕鬆串接 CI/CD，將使用者確認後的變更交給既有流水線建置、測試與部署。此構想尚未排入階段，詳見 [docs/product.md](docs/product.md#長期願景尚未排入階段)。

## 授權

尚未指定。
