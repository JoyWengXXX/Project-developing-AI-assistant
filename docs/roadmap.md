# RepoPilot 階段規劃與狀態

狀態定義：**已完成**（已實作且實際驗證）、**未驗證**（已實作但未實際驗證）、**未完成**（尚未實作）。

## 總覽

| 階段 | 名稱 | 依賴 | 狀態 |
| --- | --- | --- | --- |
| 1 | 專案骨架、Repo 管理與固定版本匯入 | 無 | 進行中（見下方切片） |
| 2 | 背景匯入與程式結構索引 | 1 | 未完成 |
| 3 | 語意與混合搜尋 | 2 | 未完成 |
| 4 | 有證據的 Repo 問答 | 3 | 未完成 |
| 5 | 需求拆解與客製化規劃 | 4 | 未完成 |
| 6 | V1 介面與評估 | 5 | 未完成 |
| 7 | V2 獨立工作目錄改碼 | 5、6 | 未完成 |
| 8 | V3 測試與有限次修正 | 7 | 未完成 |
| 9 | 部署與履歷作品整理 | 全部 | 未完成 |

## 第一階段：專案骨架、Repo 管理與固定版本匯入

範圍：專案初始化、設定、migration；註冊本地 repo；revision 解析為完整 commit SHA；讀取該 commit 的檔案樹與 blob；保存路徑、文字內容、blob ID、SHA-256、位元組大小與狀態；snapshot 與檔案查詢；去重、失敗紀錄、測試與啟動文件。

範圍外：LLM、向量索引、改碼、遠端 clone、前端、多使用者登入、背景 worker。

### 實作切片

| # | 切片 | 狀態 |
| --- | --- | --- |
| 0 | 文件骨架（README、product、roadmap、architecture） | 已完成（文件） |
| 1 | 專案骨架、設定、`/health`、request ID、結構化日誌、統一錯誤格式 | 已完成（2026-10-22：`uv run pytest` 19 passed；ruff、mypy 通過；uvicorn 手動驗證 `/health`） |
| 2 | Docker Compose（dev／test DB）、SQLAlchemy、Alembic、`/ready` | 已完成（2026-10-24：`docker compose up -d --wait db db-test`；`uv run alembic upgrade head` 於 dev 資料庫成功，`alembic current` 為 `0001 (head)`；`uv run pytest` 24 passed，含 PostgreSQL 整合測試；ruff、mypy 通過；uvicorn 手動驗證 `/ready` 回 200。目前 migration 僅為空基線，尚無資料表） |
| 3 | Repository 註冊（路徑正規化、`REPOSITORY_ROOTS`、根目錄檢查、去重） | 已完成（2026-10-26：`POST/GET /api/v1/repositories`、`GET /api/v1/repositories/{id}`；migration `0002`；`uv run pytest` 75 passed，含 PostgreSQL 整合測試與併發註冊測試；ruff、mypy、`alembic check` 通過） |
| 4 | revision 解析與快照匯入 | 未完成 |
| 5 | Snapshot 與檔案查詢（分頁、歸屬檢查、狀態過濾） | 未完成 |
| 6 | 失敗、重試、併發與邊界測試 | 未完成 |
| 7 | README、架構文件與 `docs/learning/stage-01.md` 收尾 | 未完成 |

### 驗收條件

| 驗收條件 | 狀態 |
| --- | --- |
| 重啟服務後資料仍存在 | 未完成 |
| 同 repo、同 commit、同匯入設定不產生重複快照 | 未完成 |
| 工作目錄髒資料不影響匯入 | 未完成 |
| 指定舊 commit 可正確讀取該版本 | 未完成 |
| 匯入失敗不留下可被當成成功資料的半套索引 | 未完成 |

### 最近驗證（2026-10-26，切片 3）

- `uv run pytest`：75 passed（含 PostgreSQL 整合測試：註冊 201／重複 200、路徑不同寫法去重、根目錄外／子目錄／非 repo／不存在／相對路徑 422、bare repo 拒絕、Git 逾時 504、8 執行緒併發註冊只產生一筆、唯一約束名稱）。
- `uv run ruff check .`、`uv run ruff format .`、`uv run mypy src`：通過；`uv run alembic check`（對 test 資料庫）：無未同步差異。
- 限制：symlink 逃逸測試在本機以 Windows junction 驗證（無 symlink 權限時改用），真正的 symlink 與 Linux／macOS 未驗證；「dubious ownership」訊息分支未實際驗證；`REPOSITORY_ROOTS` 以環境變數設定的實際啟動流程未手動驗證。
- 尚無快照匯入，不代表第一階段整體驗收完成。

### 先前驗證（2026-10-24，切片 2）

- `uv run pytest`：24 passed（含 3 個 PostgreSQL 整合測試：`/ready` 200、migration 從空 schema `upgrade head`、upgrade／downgrade／upgrade）；另有一則 Starlette TestClient 使用 HTTPX 的棄用警告。
- `uv run ruff check .`、`uv run ruff format --check .`、`uv run mypy src tests`：通過。
- 手動：dev 資料庫 `alembic upgrade head` 成功；uvicorn 的 `/ready` 回 200。`/ready` 在資料庫不可連線時回 503，由自動化測試涵蓋（未實際停止 dev 容器驗證）。
- 尚無任何資料表，不代表第一階段整體驗收完成。

### 先前驗證（2026-10-24，切片 1）

- `uv run pytest`：19 passed；另有一則 Starlette TestClient 使用 HTTPX 的棄用警告。
- `uv run ruff check .`：通過。
- `uv run ruff format --check .`：通過（21 個檔案無需格式化）。
- `uv run mypy src tests`：通過（17 個來源檔案）。
- 上述結果只涵蓋現有骨架；PostgreSQL、migration 與 Git 匯入尚未實作，不代表第一階段整體驗收完成。

## 第二階段：背景匯入與程式結構索引（計畫）

- [詳細規格草稿](./stage-02-spec.md)：未完成，待使用者確認；須先完成第一階段整體驗收，本次未開始第二階段實作。
- 實作：Celery + Redis；匯入 API 改回傳工作識別碼並提供進度；Python AST 抽取模組、函式、類別、import 與行號；檔案、符號與關鍵字搜尋；工作重試與冪等。
- 驗收：長時間工作不佔住 HTTP 請求；失敗可追查並安全重試；語法錯誤不造成整批索引失敗；搜尋可限定 repo 與 snapshot。

## 第三階段：語意與混合搜尋（計畫）

- 實作：文件依段落、Python 依結構切分；embedding、pgvector、模型與維度版本管理；關鍵字與向量結果合併（可先用 RRF）；固定檢索評估集。
- 驗收：比較關鍵字、向量、混合搜尋的 Recall@5 與耗時；不同版本片段不混用；更換模型不混用不相容向量。

## 第四階段：有證據的 Repo 問答（計畫）

- 實作：問題檢索、上下文組合、模型回答；引用 repo、commit、檔案與行號；資料不足時明確表達；保存模型、提示詞版本、使用片段、耗時與用量。
- 驗收：引用存在且可定位；不憑空編造；模型錯誤有明確處理；每次回答可追查來源。

## 第五階段：需求拆解與客製化規劃（計畫）

- 實作：需求轉為角色、功能、限制、驗收條件與待確認事項；工具：搜尋、讀檔、查符號、取得專案資訊；產出候選修改位置、適配理由與測試案例；限制工具次數、時間與上下文大小。
- 驗收：模糊需求列出待確認事項；主要建議可追溯至證據或明確標為推測；工具失敗會結束或降級。

## 第六階段：V1 介面與評估（計畫）

- 實作：React + TypeScript + Vite 介面；需求輸入、方案檢視、來源定位；分析歷史、錯誤與成本顯示；人工標註評估案例。
- 驗收：他人可從匯入走到取得分析報告；呈現檢索品質、引用正確性與失敗案例。

## 第七階段：V2 獨立工作目錄改碼（計畫）

- 實作：Git worktree 與專用分支；限定工具修改檔案並防止路徑越界；產生 diff 與摘要。
- 驗收：原工作目錄與主要分支不受影響；不修改參考 repo；不自動 commit、push、建 PR 或合併。

## 第八階段：V3 測試與有限次修正（計畫）

- 實作：Docker 受限執行環境；最多 3 輪修正，保存每輪 diff、錯誤與結果。
- 驗收：區分環境失敗與程式失敗；達限制確實停止；不以修改或刪除驗收測試取得通過。

## 第九階段：部署與履歷作品整理（計畫）

- 實作：Docker 化、健康檢查、CI、監控與啟動文件；示範資料與評估報告；若開放遠端使用，加入認證與資源隔離。
- 驗收：新環境能依文件啟動；履歷數字全部來自實測。

## 後續構想（未排入階段）

| 構想 | 說明 | 狀態 |
| --- | --- | --- |
| 透過系統介面串接 CI/CD | 在介面設定既有 CI/CD 連線，將使用者確認後的變更交由 CI/CD 建置、測試、部署並回顯結果；觸發外部流水線或寫入遠端前須使用者明確確認 | 未完成；排入階段與範圍待確認（可能接在第七～九階段之後） |
