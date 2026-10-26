# RepoPilot

AI 輔助專案客製化平台：管理參考 repos 與目標 repo，依自然語言需求產出附程式碼來源引用的修改方案；後續在獨立工作目錄改碼、執行測試並產出可審查的 diff 與驗證報告。

> 狀態：**第一階段實作中**（專案骨架、Repo 管理與固定版本匯入）。目前已有專案骨架、`/health`、`/ready`、PostgreSQL（Docker Compose）與 Alembic 基線；尚無 Repo 註冊與匯入功能。各階段狀態見 [docs/roadmap.md](docs/roadmap.md)。

## 文件

| 文件 | 內容 |
| --- | --- |
| [docs/product.md](docs/product.md) | 產品目標、版本範圍、長期願景 |
| [docs/roadmap.md](docs/roadmap.md) | 九個階段的目標、驗收與目前狀態 |
| [docs/architecture.md](docs/architecture.md) | 架構、資料模型與設計決策 |

## 第一階段範圍

- 註冊 `REPOSITORY_ROOTS` 允許目錄內的本地 Git repo。
- 將 revision 解析為完整 commit SHA，直接從 Git 物件讀取該 commit 的檔案樹與內容（不讀工作目錄）。
- 保存路徑、UTF-8 文字內容、blob ID、SHA-256、位元組大小與跳過原因；同 repo、同 commit、同匯入設定不重複匯入。
- 透過 REST API 查詢 repo、snapshot 與檔案。

不在第一階段：LLM、向量搜尋、改碼、遠端 clone、前端介面、背景 worker。

## 環境需求

- Python 3.12（以 uv 管理：`uv python install 3.12`）
- uv
- Docker Desktop（執行 PostgreSQL）
- Git CLI

## 常用指令

已驗證（切片 2，2026-10-24，Windows PowerShell、Docker 27.4.0）：

```powershell
uv sync
docker compose up -d --wait db db-test
uv run alembic upgrade head
uv run uvicorn repopilot.main:create_app --factory --reload
uv run pytest
uv run ruff check . ; uv run ruff format --check .
uv run mypy src tests
```

整合測試需要 `db-test` 容器（埠 5433，資料不保存）；未啟動時整合測試會明確失敗，不會被跳過。`pytest` 會重建測試資料庫的 `public` schema，且只允許資料庫名稱含 `test`。

啟動後可開啟 `http://127.0.0.1:8000/docs`（Swagger UI），或：

```powershell
curl.exe -i http://127.0.0.1:8000/health   # 程序存活，不檢查資料庫
curl.exe -i http://127.0.0.1:8000/ready    # 驗證資料庫；無法連線回 503 database_unavailable
```

### 設定

複製 `.env.example` 為 `.env` 後修改。可用設定：`APP_NAME`、`LOG_LEVEL`、`DATABASE_URL`、`DB_CONNECT_TIMEOUT_SECONDS`、`DB_POOL_SIZE`、`DB_MAX_OVERFLOW`、`DB_POOL_TIMEOUT_SECONDS`（連線池預設 5＋10、等待 30 秒，同 SQLAlchemy 預設，未經實測；池滿等待逾時時請求會失敗）；整合測試另讀環境變數 `TEST_DATABASE_URL`（不讀 `.env`，未設定時使用 `db-test` 預設值）。範例中的帳密是本機開發用假值，請勿用於共用或正式環境，也不要提交 `.env`。

### 錯誤格式

所有錯誤回應統一為：

```json
{ "error": { "code": "not_found", "message": "Not Found", "request_id": "8c20e0fba456484fa7f4fb64a8ab39da" } }
```

每個回應都帶 `X-Request-ID` 標頭；請求可自帶 `X-Request-ID`（1–64 個英數、`.`、`_`、`-`），不合法時由伺服器重新產生。

### 路徑說明

- **本機 repo 路徑**：要匯入的 Git repo（例如 `D:\repos\demo`）必須位於 `REPOSITORY_ROOTS` 設定的目錄內，由主機上的 API 服務直接讀取。
- **容器 volume**：只用來保存 PostgreSQL 資料庫檔，**不是**被匯入的 Git repo 位置。

## 長期願景

希望最終能透過系統介面輕鬆串接 CI/CD，將使用者確認後的變更交給既有流水線建置、測試與部署。此構想尚未排入階段，詳見 [docs/product.md](docs/product.md#長期願景尚未排入階段)。

## 安全原則

- 系統不自動 commit、push、建立 PR 或合併。
- 匯入時預設排除 `.env`、私鑰與憑證檔；請勿將真實金鑰放入程式、測試或文件。

## 授權

尚未指定。
