# RepoPilot

AI 輔助專案客製化平台：管理參考 repos 與目標 repo，依自然語言需求產出附程式碼來源引用的修改方案；後續在獨立工作目錄改碼、執行測試並產出可審查的 diff 與驗證報告。

> 狀態（2026-11-15）：**第一階段**實作與驗證已完成，使用者已於 2026-11-11 確認驗收。**第二階段待使用者確認**：切片 1～9 已完成，包含背景工作、Python／C# 結構索引、三種搜尋及主控台輪詢；切片 10 已整理文件並完成下列驗證，仍有未驗證項目。**參考版本只是標記，尚未連到 Agent**；LLM、向量搜尋、改碼都還不存在。不宣稱整套平台完成。未驗證項目見本文各節與本機 [roadmap](docs/roadmap.md)。

## 開發原則：多語言 repo

RepoPilot 的產品目標是適用不同程式語言的 repo，不以 Python／C# 為永久限制。目前結構索引另有
TypeScript／JavaScript 語法層級解析；其他符合匯入規則的文字檔可用檔名及文字搜尋，**不能**宣稱已解析其程式結構。
每個階段的規格與驗收都應區分各語言的實際能力、明示未支援範圍，並評估混合語言 repo；
新增解析器的語言與時程需另行確認，不假設所有語言已受支援。

第三階段（語意與混合搜尋）**進行中**：TypeScript／JavaScript 結構索引已實作，
並以隔離 worker 堆疊及瀏覽器操作驗證代表情境。Java、Go、Rust、SQL 解析及跨 repo／快照搜尋尚未實作；
IBM embedding 僅做本機試跑，尚未整合產品。

## 文件

> **`docs/` 目錄只是本機參考，已被 `.gitignore`，不在版本庫**；在 GitHub 上看這份 README 時，下列 `docs/` 連結會失效。

| 文件 | 內容 |
| --- | --- |
| [docs/learning/stage-01.md](docs/learning/stage-01.md) | **學習筆記**：主控台導覽、介面操作 → API → service → Git／資料表的追蹤表、流程圖、.NET 對照、動手練習、第二階段預覽（背景匯入） |
| [docs/learning/stage-02.md](docs/learning/stage-02.md) | **學習筆記**：背景工作、索引與搜尋的資料流、.NET 對照及練習 |
| [docs/product.md](docs/product.md) | 產品目標、版本範圍、長期願景 |
| [docs/roadmap.md](docs/roadmap.md) | 九個階段的目標、驗收與目前狀態；各切片驗證紀錄 |
| [docs/architecture.md](docs/architecture.md) | 架構、資料模型、設計決策（D1～D48）、前端主控台結構 |
| [docs/stage-02-spec.md](docs/stage-02-spec.md)、[切片 3b 規格](docs/stage-02-slice-3b-spec.md) | 第二階段規格與版本管理切片規格 |
| [切片 5c](docs/stage-01-slice-5c-spec.md)、[5d](docs/stage-01-slice-5d-spec.md)、[5e](docs/stage-01-slice-5e-spec.md) | 第一階段切片規格（GitHub 連動、倉庫總覽、版本瀏覽） |

## 目前能做什麼

- 註冊 `REPOSITORY_ROOTS` 允許目錄內的**本機 Git repo**，或以 `owner/name` 從 **GitHub** 註冊（clone 到受管目錄 `MANAGED_REPOS_DIR`；使用者自行提供 Personal Access Token，建議 fine-grained、Contents 唯讀）。
- 把 revision 解析為完整 commit SHA，**只讀 Git 物件**（不讀工作目錄）匯入成**快照**：保存路徑、UTF-8 文字內容、blob ID、SHA-256、位元組大小與跳過原因；同 repo、同 commit、同匯入設定不重複匯入；`.env`、私鑰與憑證檔不讀內容。
- **背景匯入**（第二階段）：`import-jobs` 立即回 202，由 dispatcher 與 Celery worker（Docker Linux 容器）在背景匯入；同步的 `imports` 保留但**已棄用**。
- **背景 GitHub 註冊與 fetch**（第二階段切片 5）：`registration-jobs`、`fetch-jobs` 立即回 202，由 worker 在背景 clone／fetch（可取消、有階段與暫存目錄大小進度、worker 被殺後會清理暫存目錄）；同步的 `POST /repositories/github`、`POST /repositories/{id}/fetch` 保留但**已棄用**。
- **程式結構索引**：Python（`ast`）、C#、TypeScript 與 JavaScript（tree-sitter）做語法層級解析，**不執行、不編譯、不 import** 受分析的程式，也不做跨檔語意分析。TypeScript／JavaScript 包括 `.ts`、`.tsx`、`.mts`、`.cts`、`.js`、`.jsx`、`.mjs`、`.cjs`，支援具名函式與箭頭函式、類別及方法、TS 介面／型別別名／列舉及靜態 import；語法錯誤標示部分解析。C# 的 partial 類別不合併，`#if` 分支標示條件式定義，欄位預設不抽取（`INDEX_CSHARP_INCLUDE_FIELDS=true` 可啟用）。其他語言只保存文字，不產生符號。主控台顯示索引狀態與符號樹；可透過 `POST /snapshots/{id}/index-jobs` 建立索引。新解析器改變索引設定雜湊，舊資料保留但須依需求重建；worker 映像需重新建置並重建容器才能載入新依賴。
- **參考版本**：每個 repo 可把一個已匯入的版本設為參考（固定的快照，不跟著分支前進）；**只是標記，尚未連到 Agent**。可移除已匯入的版本（含批次，只刪資料庫紀錄）。
- 查詢 repo、快照、檔案、分支與標籤、commit 歷史、repo 概況、符號與背景工作狀態；GitHub 來源可 fetch；可移除已註冊的 repo。
- **搜尋**（第二階段切片 8）：對已匯入的快照做三種字面（區分大小寫）子字串搜尋——檔名（`search/files`）、符號（`search/symbols`，需要已完成的結構索引）、內容（`search/text`，一個命中行一筆並帶片段；不依賴索引）。沒有正規表示式、模糊比對或相關性排序；沒有加 `pg_trgm`（20,000 檔合成快照的最壞情況單次約 3.6 秒，低於預設逾時 10 秒；真實 repo 與 Linux 容器內未驗證）。超過 `SEARCH_STATEMENT_TIMEOUT_SECONDS` 回 504 `search_timeout`。主控台在版本管理的「搜尋」面板使用。
- 操作主控台（`frontend/`）：repo 卡片總覽、版本管理（已匯入的版本＋分支下拉＋ commit 圖）、程式結構、搜尋、檔案樹、背景工作摘要、GitHub 連線。

**尚未有（未完成）**：LLM、向量搜尋、改碼、GitLab 與其他遠端來源、多使用者登入、完整前端自動化測試。

## 環境需求

- Python 3.12（以 uv 管理：`uv python install 3.12`）與 [uv](https://docs.astral.sh/uv/)
- Docker Desktop（PostgreSQL；背景工作堆疊另需 Redis 與 worker 容器）
- Git CLI
- Node.js 與 npm（只有操作主控台需要；以 Node 24.11.1、npm 11.6.2 實測）

## 快速開始

以下在專案根目錄的 Windows PowerShell 執行。只想用 API、不跑背景工作時，做到步驟 4 即可（此時匯入請用同步的 `imports`，或讓主控台自動回退，見下方）。

```powershell
# 1. 安裝後端依賴、啟動資料庫（db：開發，5432；db-test：整合測試，5433，資料不保存）
uv sync
docker compose up -d --wait db db-test

# 2. 建立 .env（見下一節；至少設定 REPOSITORY_ROOTS，要用 GitHub 功能再設 TOKEN_ENCRYPTION_KEY 與 MANAGED_REPOS_DIR）
#    已經有 .env 時不要再執行這行（Copy-Item 會直接覆蓋）
Copy-Item .env.example .env

# 3. 建立資料表（目前最新 migration 為 0009；升級既有資料庫也是這行）
uv run alembic upgrade head
uv run alembic current        # 預期 0009 (head)

# 4. 啟動後端（終端機 1）
uv run uvicorn repopilot.main:create_app --factory --reload
```

**降版風險**：從 `0009` 降至 `0008` 不刪資料，但若存在新語言或新種類的索引資料會拒絕降版；從 `0008` 降到 `0007` 會刪除結構索引及符號等衍生資料，升版不會自動還原。需先評估備份與重建，勿在共用資料庫試跑。

```powershell
# 5. 啟動主控台（終端機 2）；後端必須先啟動
cd frontend
npm install
npm run dev                   # 以終端機顯示的網址為準；預設 http://localhost:5173，被占用時 Vite 會改埠
```

```powershell
curl.exe -i http://127.0.0.1:8000/health   # 程序存活，不檢查資料庫
curl.exe -i http://127.0.0.1:8000/ready    # 驗證資料庫；無法連線回 503 database_unavailable
```

Swagger UI：`http://127.0.0.1:8000/docs`。背景工作堆疊（Redis、dispatcher、worker）的啟動見後文[「背景工作堆疊」](#背景工作堆疊第二階段)。

**驗證狀態**：2026-11-12 在本機實跑 `uv run alembic current`（`0007 (head)`）、`docker compose ps`（`db`、`db-test`、`redis`、`dispatcher`、`worker` 皆 healthy）、`/health` 與 `/ready`（皆 200）、前端 `npm run typecheck` 與 `npm run lint`（exit 0）。從空資料庫依本節步驟走完一次、以及「他人依本文獨立照做」**未驗證**。

## 設定（`.env`）

複製 `.env.example` 為 `.env` 後修改；**不要提交 `.env`**（已在 `.gitignore`）。`DATABASE_URL` 的預設值對應 `docker-compose.yml` 的 `db`（本機開發用假帳密，請勿用於共用或正式環境）。`.env` 範例（**全部是假值**）：

```text
REPOSITORY_ROOTS=D:\repos
MANAGED_REPOS_DIR=D:\repopilot-managed
TOKEN_ENCRYPTION_KEY=<由下方指令產生的金鑰>
# 背景工作堆疊才需要（主機路徑一律用正斜線）
WORKER_REPOSITORIES_HOST_DIR=D:/repos
WORKER_MANAGED_HOST_DIR=D:/repopilot-managed
WORKER_PATH_MAPPINGS=[{"host":"D:/repos","container":"/mnt/repos","mode":"ro"},{"host":"D:/repopilot-managed","container":"/mnt/managed","mode":"rw"}]
```

產生 `TOKEN_ENCRYPTION_KEY`（輸出貼到 `.env`；**不要把真實金鑰貼進文件或版控**；金鑰遺失或更換後，已保存的 token 無法解密，需重新提交）：

```powershell
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

| 設定 | 預設 | 說明 |
| --- | --- | --- |
| `APP_NAME`、`LOG_LEVEL` | `RepoPilot`、`INFO` | 應用程式名稱；日誌等級（單行 JSON，帶 `request_id`） |
| `DATABASE_URL`、`DB_*` | 見 `.env.example` | PostgreSQL 連線字串（psycopg 3，`postgresql+psycopg://…`）、連線逾時與連線池（預設同 SQLAlchemy，未經實測） |
| `REPOSITORY_ROOTS` | 空 | 允許註冊的**本機 repo** 根目錄，多個以 `;` 分隔；**空白＝拒絕所有本機註冊** |
| `GIT_TIMEOUT_SECONDS` | 30 | 單一 Git 指令逾時 |
| `IMPORT_*` | 見 `.env.example` | 匯入上限（單檔 1 MiB、20,000 檔、快照 200 MiB）、Git 逾時與 `importing` 逾時接手秒數、排除規則；**改任何一項都會改變 `import_profile_hash`**，舊快照不再被重用；數值未經實測。例外：`IMPORT_BLOB_BATCH_MAX_FILES`（200）與 `IMPORT_BLOB_BATCH_MAX_BYTES`（4 MiB）只決定匯入時一批讀幾個 blob（影響進度與取消的粒度，不影響結果），**不會**改變 `import_profile_hash` |
| `TOKEN_ENCRYPTION_KEY` | 空 | GitHub token 的 Fernet 加密金鑰；**空白時無法保存 token，沒有預設金鑰**。**背景 GitHub 工作（切片 5）由 worker 在容器內解密 token，所以 worker 的環境也要有同一把**（放 `.env.worker`，見下）；已保存 token 卻缺金鑰時，job 以永久性錯誤 `token_encryption_not_configured` 失敗（不重試） |
| `MANAGED_REPOS_DIR` | 空 | GitHub clone 的**受管目錄**（絕對路徑）；**空白＝停用 GitHub 註冊**；與 `REPOSITORY_ROOTS` 分開。背景註冊要求它能映射到 worker 的**可寫**掛載（`WORKER_PATH_MAPPINGS` 的 `mode` 為 `rw`），否則 503 `worker_path_not_mapped` |
| `GITHUB_*` | 見 `.env.example` | GitHub API／clone 的基底位址（僅供測試指向本機假 GitHub）與逾時（未實測，大型 repo 可能需調高） |
| `GIT_POLL_SECONDS`、`CLONE_PROGRESS_SECONDS`、`MANAGED_TEMP_ORPHAN_SECONDS` | 1、2、7200 | 切片 5（只影響 worker，未實測）：clone／fetch 期間檢查取消旗標與租約的間隔、量測暫存目錄大小（`progress.bytes_received`）的間隔、孤兒暫存目錄的年齡門檻（須大於 `GITHUB_CLONE_TIMEOUT_SECONDS`） |
| `SEARCH_STATEMENT_TIMEOUT_SECONDS` | 10 | 切片 8：每個搜尋請求的時間上限（PostgreSQL `statement_timeout`；內容搜尋另有整體期限）；超過回 504 `search_timeout`，不回空列表。預設值只在 20,000 檔合成快照上量測過一次 |
| `WORKER_PATH_MAPPINGS` | 空 | 主機路徑 ↔ 容器路徑（JSON 陣列 `[{host, container, mode}]`）；**API 建立 import-job 時用它檢查路徑可映射**，未涵蓋回 503 `worker_path_not_mapped`；容器內的設定由 docker-compose 提供，所以 **API 的 `.env` 也要設** |
| `WORKER_REPOSITORIES_HOST_DIR`、`WORKER_MANAGED_HOST_DIR` | 專案內 `.test-repos`、`.test-managed` | 只給 docker compose 插值（後端不讀）：worker 容器掛載的主機目錄 |
| `WORKER_GIT_TIMEOUT_SECONDS`、`WORKER_IMPORT_GIT_TIMEOUT_SECONDS`、`WORKER_TASK_TIME_LIMIT_SECONDS` | 120、900、3600 | worker 的 Git 與任務逾時；未實測，只影響 worker |
| `CELERY_*`、`JOB_*`、`DISPATCH_*`、`RECOVERY_SCAN_SECONDS`、`SERVICE_HEARTBEAT_SECONDS` | 見 `.env.example` | 只有 dispatcher 與 worker 使用 Celery／Redis 設定（compose 已設定）；**API 不連 Redis**。租約、重試、派送與心跳秒數皆為未實測的起點 |
| `TEST_DATABASE_URL` | `db-test` 預設值 | 只供整合測試讀取環境變數（不讀 `.env`）；資料庫名稱必須含 `test` |

選用：複製 `.env.worker.example` 為 `.env.worker`（不納入版控）放 worker 專用設定；沒有這個檔案也能啟動（compose 以 `required: false` 讀取）。**要用背景 GitHub 註冊與 fetch 且已保存 token 時，必須在 `.env.worker` 設定與 `.env` 相同的 `TOKEN_ENCRYPTION_KEY`**（`docker inspect` 看得到容器環境變數，僅限本機單人使用，見下方安全注意事項）；設定後請重建 worker 容器（`docker compose --profile jobs up -d worker`，只會重建 worker）。

### 路徑說明

- **本機 repo 路徑**：要註冊的 Git repo（例如 `D:\repos\demo`）必須位於 `REPOSITORY_ROOTS` 內，由**後端所在的主機**直接讀取；介面輸入的是後端主機上的路徑，不是瀏覽器所在機器的路徑。`MANAGED_REPOS_DIR` 內的路徑不能以本機來源註冊。
- **受管目錄** `MANAGED_REPOS_DIR`：只存放系統自己 clone 的 GitHub repo（`github\<owner>\<name>`，clone 進行中的暫存目錄在 `.tmp\`），**不要**放進 `REPOSITORY_ROOTS`，也不要指向既有的重要目錄；第一次註冊時才建立。
- **worker 容器**：看不到 Windows 路徑，所以 `REPOSITORY_ROOTS` 以**唯讀**、`MANAGED_REPOS_DIR` 以讀寫 bind mount 掛入 `/mnt/repos`、`/mnt/managed`，並以 `WORKER_PATH_MAPPINGS` 映射。
- **容器 volume**：只保存 PostgreSQL 資料庫檔，**不是**被匯入的 Git repo 位置。

## 背景工作堆疊（第二階段）

背景匯入需要 Redis、dispatcher 與 worker 三個容器（Compose profile `jobs`，不影響上面的 `db db-test`）。設計與取捨見 [architecture.md D39、D40](docs/architecture.md)。**沒有它們也能用**：API 不連 Redis，工作維持 `queued`；主控台在 worker 與 dispatcher 沒有同時在線時，會自動回退到同步的 `POST …/imports` 並說明。

```powershell
# 1. 在 .env 設好 WORKER_REPOSITORIES_HOST_DIR、WORKER_MANAGED_HOST_DIR、WORKER_PATH_MAPPINGS（見上方範例）

# 2. 建置映像（worker 與 dispatcher 共用；依 uv.lock 安裝依賴，首次約 1～2 分鐘）
docker compose --profile jobs build

# 3. 啟動：資料庫、Redis、dispatcher、worker（連開發的 db）
docker compose --profile jobs up -d --wait db redis dispatcher worker

# 4. 重啟 API（讓它讀到 WORKER_PATH_MAPPINGS），再確認兩者在線
Invoke-RestMethod http://127.0.0.1:8000/api/v1/jobs/status   # dispatcher.status、worker.status 應為 online

# 5. 停止：只停止，不要用 docker compose down（那會連 db 一起處理）
docker compose --profile jobs stop worker dispatcher redis
```

- **驗證狀態**：啟動流程與 import-job 流程（註冊 → `import-jobs` → 輪詢 → 查詢快照檔案；含 `docker compose kill worker` 後由復原掃描重排並成功）已於 2026-11-11 以拋棄式資料庫與臨時 API 實跑；上面的指令文字與實跑的相近但**沒有逐字複製執行**。2026-11-12 另確認使用者環境中這五個容器皆 healthy、`GET /api/v1/jobs/status` 回 `online`。2026-11-12（切片 5）：GitHub 來源的 worker 匯入、背景註冊與 fetch 已由 `stack` 測試在 `worker-test` 容器以本機假 GitHub 驗證（**沒有連真實 GitHub**）。**未驗證**：多個 worker／dispatcher、`docker stop` 的 warm shutdown、Redis 或 PostgreSQL 重啟、私有 repo 與真實 token。
- **R1（掛載上讀取 loose 物件很慢）**：容器讀 Windows 掛載比主機慢，從未 `git gc` 且 loose 物件極多的本機 repo 可能超過逾時（切片 1 單次實測：20,000 個 loose 物件約 391 秒，主機約 12 秒）。逾時時 job 的錯誤會提示可在該 repo 自行執行 `git gc`（RepoPilot 不會替你的 repo 執行）。
- **測試堆疊**：`redis-test`、`dispatcher-test`、`worker-test`（profile `jobs-test`）連 `db-test`，與開發的 `db`、`redis` 分屬不同 Docker 網路；由 `stack` 測試自動啟停，見下方「測試與品質檢查」。
- 切片 1 的 spike 腳本在 `scripts\spike\`（腳本檔頭有說明）；切片 3 起部分腳本已不適用，未修改也未重跑。

## 主控台操作流程

React + TypeScript + Vite 的最小操作主控台（`frontend/`），用來以點擊理解「註冊 → 匯入 → 參考 → 檔案」的流程並對照 API；只呼叫下方 API 一覽的端點，不在前端重做業務規則，也沒有登入。結構見 [architecture.md「前端主控台結構」](docs/architecture.md#前端主控台結構)，逐區塊導覽見[學習筆記](docs/learning/stage-01.md#12-主控台導覽)。

1. **新增 repo**（首頁的摺疊區）：「從 GitHub 註冊」輸入 `owner/name`，或「本機路徑註冊」輸入**後端主機上**的絕對路徑。註冊**成功（201）時自動匯入一次**預設分支最新版本；已註冊過（200）不會匯入。匯入失敗時卡片顯示錯誤與「重試匯入」，不會自動再試。
2. **倉庫總覽**：每個 repo 一張卡片（來源、預設分支最新 commit、README 擷取、語言分布、專案資訊）。預設分支最新版本**尚未匯入**時，卡片顯示「匯入」按鈕——**卡片不會自動匯入**（移除版本後也不會被自動匯回）。卡片上也有「參考版本」區（見第 5 點）、「重新整理概況」、GitHub 來源的「更新（fetch）」，以及兩段式確認的「移除」。
3. **查看詳情**（版本管理）：左側「已匯入的版本」清單（勾選、批次「移除選取的版本」；失敗與過期設定的快照會標示），右側是分支下拉、**選取 commit 的操作區**（在 commit 圖上方）與 SVG commit 圖。**點 commit 只顯示它的資訊，不會自動匯入**；按「匯入此版本」才匯入（顯示進度）。操作區另有「設為參考版本」／「取消參考」與「移除此版本」。
4. **檔案**（詳情頁下方，預設收合）：展開才逐頁載入檔案樹；只有已匯入的版本才有檔案可看。
5. **參考版本**：把某個已匯入版本設為該 repo 的參考，存在資料庫（`repository_references`）；每個 repo 最多一個、固定在那個快照，分支前進時卡片顯示「有較新版本」與「改用最新版本」。**只是標記，尚未連到 Agent**；舊版存在瀏覽器 localStorage 的「參考清單」不遷移、不再使用。
6. **背景工作**：頁面頂端只有一行摘要（進行中／失敗／背景執行器在線或離線）；進行中與失敗的工作直接顯示在它所屬的 repo 卡片與詳情版本區（失敗可「重試」或「忽略」；「忽略」只存在瀏覽器 localStorage 的 `repopilot-dismissed-jobs`，不通知後端）。完整列表按「查看所有背景工作」才會開啟（對話框）。

| 指令（在 `frontend/` 執行） | 說明 |
| --- | --- |
| `npm run dev` | 開發伺服器；把 `/api`、`/health`、`/ready` 轉發到 `http://127.0.0.1:8000`（`frontend/vite.config.ts`），所以後端**不需要、也沒有開放 CORS** |
| `npm run typecheck` | `tsc --noEmit`（app 與 vite 設定兩份 tsconfig） |
| `npm run lint` | oxlint（`create-vite` 範本預設，不是 ESLint） |
| `npm run build` | `tsc -b` 型別檢查後建置到 `frontend/dist/`（不會被提交）；建置結果沒有 proxy，需另行提供同源的 `/api`，**未驗證** |
| `node --test tests\visiblePolling.test.mjs` | 輪詢可見性與中止的純邏輯測試（需支援原生 TypeScript 型別移除的 Node.js） |

**驗證狀態**：2026-11-15 的 `typecheck`、`lint`、`build` 與上述輪詢純邏輯測試通過；另用隔離的 PostgreSQL、Redis、dispatcher 與 worker，操作 Chromium 驗證本機註冊、背景匯入、同步回退及輪詢故障注入。前端尚無完整元件或端到端自動化測試；細節見 [roadmap](docs/roadmap.md)。其他瀏覽器、窄螢幕與觸控、鍵盤與螢幕閱讀器完整操作、大量 repo 與超大歷史的效能、GitHub 註冊 201 的實際 clone＋自動匯入**未驗證**。

## API 一覽

共 39 個端點（可用 `http://127.0.0.1:8000/openapi.json` 對照；以 `create_app().openapi()` 核對為 39 個 method＋path，切片 6 新增 5 個、切片 8 新增 3 個）。契約與取捨見 [architecture.md](docs/architecture.md#設計決策) 的 D 編號。

| 方法 | 路徑 | 說明 | 主要狀態碼與錯誤碼 |
| --- | --- | --- | --- |
| GET | `/health` | 程序存活，不檢查資料庫 | 200 |
| GET | `/ready` | 驗證資料庫連線 | 200；503 `database_unavailable` |
| POST | `/api/v1/repositories` | 註冊本機 repo（body `{"path": "...", "name": "..."}`，`name` 可省略） | 201 新建；200 已註冊（路徑不同寫法會去重）；422 `invalid_repository_path` |
| GET | `/api/v1/repositories` | repo 列表 `{items}`（含 `source_type`、`github_*` 欄位） | 200 |
| GET | `/api/v1/repositories/{id}` | 單一 repo | 200；404 `repository_not_found`；422 `validation_error`（id 超出 1～2³¹−1） |
| DELETE | `/api/v1/repositories/{id}` | 移除 repo 與其快照、檔案；**本機來源只刪紀錄、絕不碰磁碟**；GitHub 來源另刪受管目錄內的 clone；有 `queued`／`running` 的工作（任何種類）或 repo 鎖被持有（匯入、clone、fetch）時拒絕 | 200 `{repository_id, source_type, deleted_snapshots, deleted_files, removed_clone}`；404；409 `repository_busy`；422；500 `managed_storage_error`；503 `github_registration_disabled`（[D33](docs/architecture.md)、[D44](docs/architecture.md)） |
| POST | `/api/v1/repositories/github` | **【已棄用 deprecated】**以 `owner/name`（或 `https://github.com/owner/name`）**同步**註冊並 clone（body `{"repo": "..."}`）；請求會占用到 clone 結束，請改用 `registration-jobs`；與 worker 共用 repo 鎖（鎖忙碌時等待，逾時 409） | 201；200 已註冊（不重新 clone）；409 `repository_busy`；422 `invalid_github_repo`、`github_token_invalid`；404 `github_repo_not_found_or_private`；503 `github_registration_disabled`；502 `github_clone_failed`、`github_unreachable`、`github_api_error`；504 `github_clone_timeout`、`github_timeout`；500 `managed_storage_error`（[D32](docs/architecture.md)） |
| POST | `/api/v1/github/registration-jobs` | **背景 GitHub 註冊**（切片 5）：body `{"repo": "owner/name"}`（同上）；API 只做不需網路的驗證（格式、`MANAGED_REPOS_DIR`、受管目錄可映射到 worker 的可寫掛載），查 GitHub、clone 與寫入資料列由 worker 執行；成功後 `GET /jobs/{id}` 的 `result.repository_id` 是新 repo 的 id。同一 owner/name（不分大小寫）進行中只有一個 job | 202 `{job, snapshot_id: null, repository_id: null, reused: false}`＋`Location`；200 `{job: null, repository_id, reused: true}`（已註冊，不建立 job）；422 `invalid_github_repo`；503 `github_registration_disabled`、`worker_path_not_mapped`；GitHub 相關錯誤碼成為 job 的 `error.code`（[D44](docs/architecture.md)） |
| POST | `/api/v1/repositories/{id}/fetch` | **【已棄用 deprecated】**僅 GitHub 來源：**同步** `git fetch --prune origin`，回報 ref 變動；不建立快照；請改用 `fetch-jobs` | 200 `{repository_id, updated_refs, truncated}`；409 `repository_not_github`、`repository_busy`；502 `github_fetch_failed`；504 `github_fetch_timeout`；422 |
| POST | `/api/v1/repositories/{id}/fetch-jobs` | **背景 fetch**（切片 5）：僅 GitHub 來源；`job.result.updated_refs` 的 `name` 可直接作為匯入的 revision（`origin/main`）；進行中（`queued`／`running`）只有一個，結束後可再提交 | 202 `{job, snapshot_id: null, repository_id, reused: false}`＋`Location`；404；409 `repository_not_github`；422 `invalid_repository_path`；503 `github_registration_disabled`、`worker_path_not_mapped` |
| POST | `/api/v1/repositories/{id}/import-jobs` | **背景匯入**（切片 3）：body `{"revision": "main"}`，可省略＝`HEAD`；API 先在主機端把 revision 解析成完整 SHA（無效回 422、不建立 job）並檢查路徑可映射到 worker 容器，再同一交易建立 job 與待派送事件；以 `GET /jobs/{id}` 輪詢 | 202 `{job, snapshot_id, repository_id, reused}`＋`Location`（新建、重新排入失敗或取消的同一個 job、或回既有進行中的 job；**快照已完成但目前設定的索引缺少或失敗時也是 202**，匯入階段重用快照、只做索引）；200 `reused=true`（相同快照與目前設定的索引都已完成）；404；409 `snapshot_import_in_progress`；422 `invalid_revision`、`invalid_repository_path`；503 `worker_path_not_mapped`（[D40](docs/architecture.md)、[D45](docs/architecture.md)） |
| POST | `/api/v1/repositories/{id}/imports` | **【已棄用 deprecated】**同步匯入固定版本快照（body 同上）；請求會占用到匯入結束，長時間匯入請改用 `import-jobs`；與 worker 共用快照鎖 | 201 新建；200 相同快照已完成；409 `snapshot_import_in_progress`；422 `invalid_revision`、`invalid_repository_path`、`import_limit_exceeded`；504 `git_timeout` |
| GET | `/api/v1/repositories/{id}/snapshots` | 快照列表（新的在前；含 `importing`／`failed`；`?status=`、`limit`、`offset`）；每筆另有 `is_referenced`、`is_stale_profile`（匯入設定與目前不同） | 200 `{items, total, limit, offset}` |
| GET | `/api/v1/repositories/{id}/refs` | 分支與標籤（唯讀 Git；本機來源列 `refs/heads/*`，GitHub 來源列 `origin/*`） | 200 `{default_ref, head_sha, branches, tags, truncated}`；404；422；504 `git_timeout` |
| GET | `/api/v1/repositories/{id}/commits` | commit 歷史（`--topo-order`；`ref`、`limit` 預設 100 最大 500、`offset`）；`ref` 須是 `/refs` 回傳的完整 ref | 200 `{items, total_returned, has_more}`；422 `invalid_revision`、`validation_error`（[D34](docs/architecture.md)） |
| GET | `/api/v1/repositories/{id}/overview` | 預設分支最新 commit 的概況：統計、語言分布、README 前 4,000 字元、專案資訊；**唯讀、不匯入** | 200（`analyzed=false` 時只有 `head`）；404；422；504（[D35](docs/architecture.md)） |
| GET | `/api/v1/references` | 所有 repo 的**參考版本**（只讀資料庫；只是標記，**尚未連到 Agent**） | 200 `{items: [{id, repository, snapshot_id, commit_sha, snapshot_status, file_count, skipped_count, total_bytes, created_at}]}` |
| PUT | `/api/v1/repositories/{id}/reference` | 把 repo 的某個 `completed` 快照設為參考版本（body `{"snapshot_id": N}`；每個 repo 最多一個，重複設定冪等） | 200 參考；404 `repository_not_found`、`snapshot_not_found`（含別的 repo 的快照）；409 `snapshot_not_completed`；422 |
| DELETE | `/api/v1/repositories/{id}/reference` | 取消參考版本（冪等） | 204；404 `repository_not_found`；422 |
| GET | `/api/v1/snapshots/{id}` | 快照詳情與 `file_status_counts`、`skip_reason_counts`（只有 `completed` 才統計）；另有 `is_referenced`、`is_stale_profile` | 200；404 `snapshot_not_found` |
| DELETE | `/api/v1/snapshots/{id}?force=false` | **移除快照**：只刪資料庫紀錄，**不動 Git repo**；匯入中、進行中的 job、快照鎖被持有 → `snapshot_busy`（`force` 無法繞過）；是參考版本且未帶 `force=true` → `snapshot_referenced`；移除後同一 commit 可重新匯入 | 200 `{snapshot_id, repository_id, deleted_files, was_referenced}`；404 `snapshot_not_found`；409 `snapshot_busy`、`snapshot_referenced`；422（[D41](docs/architecture.md)） |
| GET | `/api/v1/snapshots/{id}/files` | 檔案列表（不含 `content`；`status`、`path_prefix`、`limit`、`offset`） | 200 `{items, total, limit, offset}`；409 `snapshot_not_completed` |
| GET | `/api/v1/snapshots/{id}/files/{file_id}` | 單檔含 `content`（`skipped` 為 `null`，帶 `skip_reason`） | 200；404 `snapshot_file_not_found`；409 `snapshot_not_completed`（[D30](docs/architecture.md)） |
| POST | `/api/v1/snapshots/{id}/index-jobs` | **背景建立結構索引**（切片 6）：只讀資料庫裡的快照內容（不碰 Git）；同快照、同索引設定進行中只有一個 job；失敗或取消後再提交會重新排入同一個 job | 202＋`Location`；200 `reused=true`（目前設定的索引已完成）；404 `snapshot_not_found`；409 `snapshot_not_completed`（[D45](docs/architecture.md)） |
| GET | `/api/v1/snapshots/{id}/index` | 快照在**目前設定**下的索引狀態：`none`（沒有索引，200 不是錯誤）、`building`、`completed`（另有 `counts`：依語言、解析狀態、符號種類）、`failed`；`analyzers` 是抽取器與版本 | 200；404；409 `snapshot_not_completed` |
| GET | `/api/v1/snapshots/{id}/index/files` | 被索引的檔案與解析狀態（`has_problems`、`language`、`limit`、`offset`；依 path 排序）；只有第一個語法錯誤的位置，**沒有程式碼片段** | 200；409 `snapshot_not_completed`、`snapshot_index_not_ready`（`details.index_status`） |
| GET | `/api/v1/snapshots/{id}/symbols` | 符號列表（`kind`、`parent_id`、`top_level`、`file_id`、`path_prefix`、`limit`、`offset`；依 path、起始行排序；`has_children` 供符號樹逐層載入）；只採用 `completed` 的索引 | 200；409 `snapshot_not_completed`、`snapshot_index_not_ready` |
| GET | `/api/v1/snapshots/{id}/symbols/{symbol_id}` | 單一符號：位置（欄位是 0-based UTF-8 位元組位移）、修飾詞、裝飾器起始行、同檔 import 與關係（`resolved_same_file`／`ambiguous`／`unresolved`，未解析者保留原始名稱） | 200；404 `symbol_not_found`（含別的快照的符號）；409（同上） |
| GET | `/api/v1/snapshots/{id}/search/files` | **搜尋檔名**（切片 8）：路徑含 `q`（字面子字串、區分大小寫，`%`／`_`／`\` 按字面；`q` 1～200 字元、不 strip）；`path_prefix`、`limit`（預設 20、最大 100）、`offset`（0～10,000）；依 path 排序；`total` 為整數；不依賴結構索引 | 200 `{snapshot_id, repository_id, commit_sha, items, total, limit, offset, has_more}`；422 `validation_error`；404；409 `snapshot_not_completed`；504 `search_timeout`（[D47](docs/architecture.md)） |
| GET | `/api/v1/snapshots/{id}/search/symbols` | **搜尋符號**（切片 8）：`name` 或 `qualified_name` 含 `q`；`kind`、`language` 篩選；依 path、起始行排序；`total` 為整數；只採用 `completed` 的索引 | 200；409 `snapshot_not_completed`、`snapshot_index_not_ready`；422；504 |
| GET | `/api/v1/snapshots/{id}/search/text` | **搜尋內容**（切片 8）：一個命中行一筆（`path`、`line`、`snippet` 最多 300 字元、`snippet_truncated`、`snippet_offset`、`match_offset`，位移以 Unicode 碼點計）；`path_prefix` 可縮小範圍；**`total` 為 `null`**，以 `has_more` 分頁；不依賴結構索引；片段是 repo 內容，須當純文字顯示 | 200；422；404；409 `snapshot_not_completed`；504 `search_timeout`（最壞情況掃完整個快照；20,000 檔／132 MiB 的合成快照單次約 3.6 秒） |
| PUT | `/api/v1/github/token` | 驗證（呼叫 GitHub `GET /user`）後加密保存 token（body `{"token": "..."}`） | 200 `{authenticated, login, token_type, warning}`；422 `validation_error`、`github_token_invalid`；503 `token_encryption_not_configured`；502／504 |
| GET | `/api/v1/github/auth` | 只回狀態，**永不回傳 token**；不呼叫 GitHub | 200 |
| DELETE | `/api/v1/github/auth` | 刪除本機保存的 token（不在 GitHub 端撤銷） | 204（冪等） |
| GET | `/api/v1/jobs` | 背景工作列表（新的在前；`status`＝`queued`／`running`／`succeeded`／`failed`／`cancelled`／`active`、`kind`、`repository_id`、`snapshot_id`、`limit`、`offset`） | 200 `{items, total, limit, offset}`；422 |
| GET | `/api/v1/jobs/status` | dispatcher／worker 是否在線（讀 `service_heartbeats`，**不連 Redis**；`online`／`stale`／`absent`）與 `queued`／`running` 數量 | 200 |
| GET | `/api/v1/jobs/{job_id}` | 單一工作（階段、進度、嘗試次數、安全的 `error`、`result`、取消請求時間 `cancel_requested_at`、失敗追查摘要 `diagnostics`；**不含租約令牌**）。背景匯入的 `progress.processed_files` 隨每一批 blob 讀完而前進；`total_files` 為 `null` 時（還在列舉檔案）請不要顯示百分比 | 200；404 `job_not_found`；422（id 超出 1～2⁶³−1） |
| POST | `/api/v1/jobs/{job_id}/cancel` | 取消：`queued` 立即 `cancelled`；`running` 只記錄取消請求，worker 在目前這一批 blob 讀完後中止（最多延遲一批的時間，未實測大型 repo），快照標為 `failed`、不留檔案；已取消者冪等 | 200 已取消；202 已記錄請求；404；409 `job_not_cancellable` |
| POST | `/api/v1/jobs/{job_id}/retry` | 重試 `failed` 的工作（同一 job id，重新排入佇列） | 202；404；409 `job_not_retryable` |

通則：

- 錯誤格式一律為 `{"error": {"code": "...", "message": "...", "request_id": "..."}}`（422 另有 `details`，不回顯輸入）；每個回應帶 `X-Request-ID`（請求可自帶 1～64 個英數、`.`、`_`、`-`，不合法時由伺服器重新產生）。
- 列表分頁參數 `limit`（預設 50、最大 200）與 `offset`（預設 0），超出範圍回 422，不靜默截斷；路徑中的 id 限制在資料庫欄位範圍，超出回 422。
- 匯入、GitHub 註冊與 fetch 各有背景版本（`import-jobs`、`registration-jobs`、`fetch-jobs`）；同步的 `imports`、`POST /repositories/github`、`POST /repositories/{id}/fetch` 保留原契約但已標示 deprecated。移除 repo 仍是同步請求。job 端點只查詢與操作 job 狀態，不執行工作（工作由 worker 執行）。

### PowerShell 範例

路徑與 id 都是假的（`D:\repos\demo`）。**驗證狀態**：下方標為 GET 的範例（列表、`/refs`、`/commits`、`/overview`、快照與檔案、`/references`、`/jobs`、`/jobs/status`、`/github/auth`）已實跑（只讀，對使用者已在執行的後端；最近一次 2026-11-12）。**POST／PUT／DELETE 範例的文字沒有逐字實跑（未驗證）**；對應流程已由自動化測試與臨時 API＋拋棄式資料庫的手動煙霧測試驗證（第一階段 2026-11-10、`import-jobs` 2026-11-11、參考版本與移除快照 2026-11-12，見 [roadmap](docs/roadmap.md)），`snapshot_busy` 只有自動化測試。GitHub 私有 repo 與真實 token 流程**未驗證**。

```powershell
$api = "http://127.0.0.1:8000/api/v1"

# 註冊本機 repo（路徑須在 REPOSITORY_ROOTS 內）。POST，未驗證文字
$repo = Invoke-RestMethod -Method Post -Uri "$api/repositories" -ContentType "application/json" `
  -Body (@{ path = "D:\repos\demo" } | ConvertTo-Json)

# 背景匯入（需先啟動背景工作堆疊並設定 WORKER_PATH_MAPPINGS）。POST＋輪詢 GET，未驗證文字
$job = Invoke-RestMethod -Method Post -Uri "$api/repositories/$($repo.id)/import-jobs" `
  -ContentType "application/json" -Body '{"revision": "main"}'      # 202：立即回 job（status=queued）；快照已完成時 200、reused=true
do {
  Start-Sleep -Seconds 1
  $j = Invoke-RestMethod "$api/jobs/$($job.job.id)"                 # status：queued → running → succeeded／failed／cancelled
  "{0} {1} {2}/{3}" -f $j.status, $j.phase, $j.progress.processed_files, $j.progress.total_files
} while ($j.status -in "queued", "running")
$snapId = $j.snapshot_id                                            # succeeded 後有值

# 沒有背景工作堆疊時：同步匯入（已棄用，但可用）。POST，未驗證文字
$snap = Invoke-RestMethod -Method Post -Uri "$api/repositories/$($repo.id)/imports" `
  -ContentType "application/json" -Body '{"revision": "main"}'
$snapId = $snap.id
```

```powershell
# 查詢快照與檔案（GET，唯讀；不含 content，單檔才有）
Invoke-RestMethod "$api/snapshots/$snapId"                           # file_status_counts、skip_reason_counts
$page = Invoke-RestMethod "$api/snapshots/$snapId/files?path_prefix=src/&limit=20"
$page.items | Select-Object id, path, status
Invoke-RestMethod "$api/snapshots/$snapId/files/$($page.items[0].id)"

# 分支、commit 與概況（GET，唯讀）
$refs = Invoke-RestMethod "$api/repositories/$($repo.id)/refs"
$ref = [uri]::EscapeDataString($refs.default_ref)                   # ref 須是 /refs 回傳的完整名稱
(Invoke-RestMethod "$api/repositories/$($repo.id)/commits?ref=$ref&limit=20").items | Select-Object sha, subject, imported
$o = Invoke-RestMethod "$api/repositories/$($repo.id)/overview"     # analyzed=false 時請先匯入

# 背景工作（GET，唯讀）
Invoke-RestMethod "$api/jobs/status"                                # dispatcher／worker：online／stale／absent（讀心跳，不連 Redis）
Invoke-RestMethod "$api/jobs?status=active&limit=20"                # 進行中的工作（新的在前）
Invoke-RestMethod "$api/jobs/1"                                     # 單一工作；不存在回 404 job_not_found
```

```powershell
# 參考版本與移除快照（參考只是標記，尚未連到 Agent）。需要 repo 已有 completed 快照（$snapId）。PUT／DELETE，未驗證文字
Invoke-RestMethod -Method Put -Uri "$api/repositories/$($repo.id)/reference" -ContentType "application/json" `
  -Body (@{ snapshot_id = $snapId } | ConvertTo-Json)                # 200；快照須屬於該 repo 且 completed；重複設定冪等
(Invoke-RestMethod "$api/references").items | Select-Object id, snapshot_id, commit_sha, @{ n = "repo"; e = { $_.repository.name } }   # GET
(Invoke-RestMethod "$api/repositories/$($repo.id)/snapshots").items | Select-Object id, status, is_referenced, is_stale_profile        # GET

# 移除快照：只刪資料庫紀錄，不動 Git repo。它是參考版本時 409 snapshot_referenced，帶 force=true 才會一併取消參考並移除
try { Invoke-RestMethod -Method Delete -Uri "$api/snapshots/$snapId" } catch { $_.ErrorDetails.Message }
Invoke-RestMethod -Method Delete -Uri "$api/snapshots/${snapId}?force=true"       # 200 {snapshot_id, repository_id, deleted_files, was_referenced}
Invoke-RestMethod -Method Delete -Uri "$api/repositories/$($repo.id)/reference"   # 取消參考；本來就沒有也成功（204，冪等）

# 取消與重試背景工作。POST，未驗證文字
Invoke-RestMethod -Method Post -Uri "$api/jobs/1/cancel"            # queued 立即取消（200）；running 只記錄請求（202）
Invoke-RestMethod -Method Post -Uri "$api/jobs/1/retry"             # 只有 failed 可重試（202）；否則 409 job_not_retryable
```

```powershell
# GitHub：貼 token（用 Read-Host 避免進入命令歷史；真實 token 不要貼進文件或腳本）。PUT／POST／DELETE，未驗證文字
$secure = Read-Host "GitHub token" -AsSecureString
$plain = [System.Net.NetworkCredential]::new("", $secure).Password
Invoke-RestMethod -Method Put -Uri "$api/github/token" -ContentType "application/json" -Body (@{ token = $plain } | ConvertTo-Json)
Remove-Variable plain, secure
Invoke-RestMethod "$api/github/auth"                                # GET；只回狀態

# GitHub：註冊公開 repo（不需要 token）→ fetch；fetch 之後用 origin/<分支> 匯入新 commit
# 背景版本（建議；需要背景工作堆疊）：提交後輪詢 job，成功時 result.repository_id 是新 repo 的 id。POST＋GET，未驗證文字
$reg = Invoke-RestMethod -Method Post -Uri "$api/github/registration-jobs" -ContentType "application/json" `
  -Body (@{ repo = "octocat/Hello-World" } | ConvertTo-Json)      # 202；已註冊時 200 且 job 為 null
do { Start-Sleep 2; $job = Invoke-RestMethod "$api/jobs/$($reg.job.id)" } while ($job.status -in "queued", "running")
$job.status, $job.progress.bytes_received                         # succeeded；bytes_received 是暫存目錄大小（非百分比）
$ghId = $job.result.repository_id
$fetch = Invoke-RestMethod -Method Post -Uri "$api/repositories/$ghId/fetch-jobs"   # 202；job.result.updated_refs 在成功後出現
Invoke-RestMethod -Method Post -Uri "$api/repositories/$ghId/import-jobs" -ContentType "application/json" -Body '{"revision": "origin/master"}'
# 同步版本（已棄用，保留給腳本與沒有背景工作堆疊的情況）
$gh = Invoke-RestMethod -Method Post -Uri "$api/repositories/github" -ContentType "application/json" `
  -Body (@{ repo = "octocat/Hello-World" } | ConvertTo-Json)
Invoke-RestMethod -Method Post -Uri "$api/repositories/$($gh.id)/fetch"
Invoke-RestMethod -Method Post -Uri "$api/repositories/$($gh.id)/import-jobs" -ContentType "application/json" -Body '{"revision": "origin/master"}'

# 移除 repo（會刪除快照；GitHub 來源另刪本機 clone；本機來源不碰磁碟）
Invoke-RestMethod -Method Delete -Uri "$api/repositories/$($gh.id)"
```

注意：

- **fetch 之後請用 `origin/<分支>` 匯入新 commit**：clone 時建立的本機 `HEAD` 不會被 fetch 移動；主控台的版本區使用 `/refs` 列出的名稱。
- clone 是完整 clone（非 shallow）、`--no-checkout`（受管目錄內只有 `.git`）；大型 repo 佔用時間與磁碟，逾時由 `GITHUB_CLONE_TIMEOUT_SECONDS` 限制，過大 repo 的行為未實測。
- `owner`／`name` 的驗證比 GitHub 實際允許的更窄：名稱不得以 `-` 開頭、不得以 `.` 結尾（以 `.` 開頭可以，例如 `.github`），也不接受 Windows 保留名稱（`con`、`nul`、`com1` 等）。
- Windows PowerShell 5.1 的 `Invoke-RestMethod` 可能把 UTF-8 中文顯示成亂碼（回應沒有 charset），只是顯示問題；用 `curl.exe` 或 PowerShell 7 可正常顯示。
- 同一個 GitHub repo 的 clone、fetch、匯入與移除由 PostgreSQL advisory lock 在**跨程序**序列化（切片 5，D44；取代原本只在單一程序內有效的鎖）：同步的註冊與 fetch 遇到鎖忙碌時等待（逾時回 409 `repository_busy`），worker 取不到鎖則延後重排、不消耗重試額度。多個 API 程序同時註冊同一個 repo 只會有一個 clone（以真的多程序測試驗證）。

### 自行驗證私有 GitHub repo（**未驗證**，需要你自己的 token）

私有 repo 與真實 token 的 clone／fetch 目前沒有驗證紀錄（沒有真實 token 可測）。token 只在你的機器上輸入：

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

註冊回 404 `github_repo_not_found_or_private`：token 看不到該 repo（未涵蓋、權限不足，或組織要求核准）；回 422 `github_token_invalid`：token 無效或已過期，請重新提交。

## 測試與品質檢查

**注意**：整合測試會重建測試資料庫的 `public` schema；`stack` fixture 還會清空測試 Redis、
`.test-repos`、`.test-managed`、`.test-remote` 的內容，並啟停測試容器。資料庫名稱含 `test`
不代表資料可刪除。共用環境請先隔離資料庫、Redis、Compose 專案及上述目錄，不要直接使用共用資源。

```powershell
uv run pytest                      # 完整套件（含 stack 測試）
uv run pytest -m "not stack"       # 不需要 Redis／worker 容器的測試
uv run pytest -m "not slow"        # 略過啟動真實 uvicorn 子程序的重啟持久化測試
uv run pytest tests/unit -q        # 只跑單元測試（不需要資料庫）
uv run ruff check . ; uv run ruff format --check .
uv run mypy src tests
uv run alembic check               # model 與 migration 無差異（以 DATABASE_URL 指向的資料庫為準）
```

- 整合測試使用真實 PostgreSQL（預設 `db-test`，埠 5433）；**資料庫沒啟動時整合測試會明確失敗，不會被跳過**。`pytest` 會重建所選測試資料庫的 `public` schema，且只檢查資料庫名稱含 `test`；此檢查不能保護共用資料。
- **`stack` 測試**（`tests/integration/stack/`）需要真實的 Redis、dispatcher 與 worker 容器，預設**會執行**；前置是 `docker compose up -d --wait db-test` 與 `docker compose --profile jobs build`。測試自己啟動與停止 `redis-test`、`dispatcher-test`、`worker-test`；Docker 不可用或映像沒建置時**明確失敗，不跳過**。只想跑不需要容器的測試用 `-m "not stack"`。
- `tests/integration/test_restart_persistence.py` 會在 `db-test` 容器內建立臨時資料庫並啟動真實 uvicorn 子程序（空閒埠，不碰 8000；結束即 DROP 與停止），以 `slow` marker 標記，預設仍會執行。
- **最近一次完整結果**（2026-11-15，第二階段切片 10）：在獨立程式碼副本、專用 PostgreSQL 與 Compose 專案，以既有虛擬環境執行 `python -m pytest`：**2325 passed、1 warning**（1787.16 秒，含 stack）；警告為 Starlette `TestClient` 使用 `httpx` 的棄用警告。Ruff check／format、mypy、前端 3 項輪詢測試與 `typecheck`／`lint`／`build` 通過。空資料庫、`0005` 升級至 `0008`、`0008` 降版再升版均在隔離資料庫驗證；已完成 job 與索引在 API、worker、dispatcher、Redis 重啟後仍可查詢。**未驗證**：PostgreSQL 重啟、上述服務同時重啟時進行中工作的復原、真實 GitHub、取消／重試工作的完整畫面；其他限制見本機 [roadmap](docs/roadmap.md)。開發資料庫與開發 worker 未由本次測試升級。
- **第三階段 TS／JS 切片**（2026-11-15）：獨立副本與隔離 PostgreSQL 的 `python -m pytest -m "not stack" -q` 重跑 **2324 passed、18 deselected、1 warning**；首次執行的 6 項失敗為舊 fixture 仍將 `.js` 視為未支援，修正後重跑通過。真實 worker 的索引 stack 測試 3 passed；完整 stack 未執行。另以隔離 API、資料庫與瀏覽器驗證符號及搜尋代表情境。
- 前端僅有輪詢純邏輯自動化測試，尚無完整元件或端到端自動化測試（見「主控台操作流程」的驗證狀態）。

## 安全注意事項

- **GitHub token**：只加密保存於資料庫（Fernet），不進命令列、日誌、回應、前端儲存與 `.git/config`；API 永不回傳它。token 會從瀏覽器經 HTTP 送到後端，**僅限本機 `127.0.0.1` 使用，不得在未加 TLS 的遠端部署使用**；目前沒有登入與資源隔離，不要對外網開放。
- **私有 repo 的內容會存進本機資料庫**（快照內容）；請知悉資料庫保存了私有程式碼，並妥善保護資料庫與備份。
- 匯入預設排除 `.env`、私鑰與憑證檔（只記路徑與原因，不讀內容）；repo 內容視為不可信資料。請勿將真實金鑰放入程式、測試或文件，也不要提交 `.env`、`.env.worker`。
- 預設資料庫帳密（`docker-compose.yml`）是本機開發用假值，不可用於共用或正式環境。
- worker 容器能讀取你以 bind mount 掛入的目錄（`REPOSITORY_ROOTS` 唯讀）；`docker inspect` 看得到容器的環境變數（含 `.env.worker` 內容），僅限本機單人使用。
- 系統不自動 commit、push、建立 PR 或合併；匯入與瀏覽只讀 Git 物件與 refs，不修改你的本機 repo；移除快照只刪資料庫紀錄。

## 長期願景

希望最終能透過系統介面輕鬆串接 CI/CD，將使用者確認後的變更交給既有流水線建置、測試與部署。此構想尚未排入階段，詳見 [docs/product.md](docs/product.md#長期願景尚未排入階段)。

## 授權

尚未指定。
