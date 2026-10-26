# 第二階段規格草稿：背景匯入與程式結構索引

> 狀態：**未完成，待使用者確認**。2026-10-24 草擬；本文件不是功能完成紀錄。
> 前置條件：第一階段整體驗收完成。目前只完成第一階段切片 1，不得直接開始本階段實作。
> 依據：[階段規劃](./roadmap.md)、[產品目標](./product.md)。

## 1. 目標與範圍

讓使用者提交固定版本匯入後立即取得工作識別碼，查詢可追蹤的背景進度；完成後可搜尋該 snapshot 的檔案、Python 符號與文字內容。

本階段新增：

- Celery worker 與 Redis broker；PostgreSQL 保存工作、派送與索引狀態，不依賴 Redis result backend 作為事實來源。
- 背景匯入與 Python `ast` 索引、工作查詢、安全重試、合作式取消與中斷復原。
- Python 模組、類別、函式、非同步函式、import 與可辨識靜態關係。
- 限定 repo、snapshot 的檔案、符號、關鍵字搜尋。

不包含：embedding、pgvector、語意或混合搜尋、LLM、其他語言結構解析、完整呼叫圖、前端、遠端 clone、改碼或任意程式執行。

## 2. 開工前置條件

第一階段必須先完成並實際驗證：

1. PostgreSQL 開發／測試環境、SQLAlchemy 與從空資料庫執行 Alembic migration。
2. Repository 註冊、允許根目錄與 repo 根目錄檢查。
3. 固定完整 commit SHA 的 Git tree／blob 匯入、資源上限、秘密排除。
4. Snapshot 唯一約束、失敗紀錄、重試及 completed 狀態可見性。
5. Snapshot／檔案查詢與 PostgreSQL 真實整合測試。

本草稿中的欄位與路由須在第一階段完成後對照實際契約，避免以規劃取代既有實作。

## 3. 使用者流程與 API 草案

以下為**擬議契約，尚未實作**；沿用第一階段統一錯誤格式與 request ID。

| 方法 | 路徑 | 擬議行為 |
| --- | --- | --- |
| POST | `/api/v1/repositories/{repository_id}/imports` | 接受 `revision`（預設 `HEAD`），短交易建立或取得工作；回 `202`、`job_id`、`status`、`status_url` 與 `Location` |
| GET | `/api/v1/jobs/{job_id}` | 回工作階段、進度、時間、安全錯誤碼與可用的 snapshot ID |
| POST | `/api/v1/jobs/{job_id}/retry` | 僅失敗工作可重試；回 `202` 與原工作 ID，增加 attempt；執行中或已成功回 `409` |
| POST | `/api/v1/jobs/{job_id}/cancel` | queued／running 工作記錄取消要求，回 `202`；已取消回 `200`，已成功／失敗回 `409` |
| GET | `/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/symbols` | 分頁查詢完成索引的 Python 符號 |
| GET | `/api/v1/repositories/{repository_id}/snapshots/{snapshot_id}/search` | 必填 `q`；`kind=files\|symbols\|text`（預設 text）；分頁搜尋 |

### 固定版本與相容性

- API 在排入工作前解析 revision 為完整 commit SHA；不在 HTTP 請求內走訪整棵 tree 或讀取所有 blob。
- Worker 只接受資料庫工作 ID，從資料庫讀取經驗證的 repo、SHA 與設定，不重新解析 `HEAD`。
- Git 解析仍可能短暫阻塞；沿用 Git 指令逾時，不能宣稱 API 完全不執行 Git。
- 相同 repo、commit、匯入設定及索引設定重複提交回既有工作；未結束回 `202`，成功回 `200`，失敗回 `409` 並提供明確重試指引。
- 已取消工作允許以同一匯入 API 重新排程原工作；增加 attempt，不能繞過唯一約束另建重複工作。
- 已有 completed snapshot 時重用內容，只補需要的索引，不重複匯入。
- 第一階段同步匯入回 `201` 的契約將改變；實作時同步更新 OpenAPI、README、呼叫端與契約測試，不保留第二套同步匯入路徑。
- 資源不存在或 snapshot 不屬於指定 repo 回 `404`；索引未完成時，搜尋與符號查詢回 `409`，不回傳部分結果。

## 4. 背景工作與一致性

### 狀態與進度

- 工作狀態：`queued → running → succeeded / failed / cancelled`。
- 工作階段：`pending`、`importing`、`indexing`、`completed`；狀態與階段分開。
- 進度提供 `processed_files`、可未知的 `total_files`、`indexed_files`、`parse_error_count`；未知總數不偽造百分比。
- 保存 attempt、heartbeat、lease 到期時間與本次執行 token。預設最多 3 次自動執行，使用退避；這是擬議保護上限，不是實測結論。
- 只自動重試暫時性基礎設施錯誤；無效路徑、無效 revision、資源超限不自動重試。
- API 的 request ID 保存為來源關聯；worker 日誌另外帶 job ID、attempt 與階段。

### 派送、冪等與中斷

1. 建立工作與待派送事件使用同一 PostgreSQL 交易（transactional outbox）。
2. 可重啟的 dispatcher 掃描待派送事件並送至 Redis；確認 broker 接受後標記已派送。派送後、標記前崩潰可能重送，必須允許。
3. Worker 以資料庫條件更新取得租約；訊息重送不能讓同一工作同時寫入。
4. 每次寫入與完成轉換都驗證執行 token；租約過期後的舊 worker 不得覆寫新 attempt。
5. 定期復原流程處理過期租約與長時間未被接收的派送工作：未達上限重新排程，達上限標記失敗，不能永久卡住 running／queued。
6. 訊息只帶工作 ID；broker 無法派送時保留 queued 與待派送事件，日誌明確記錄失敗，不假裝工作已執行。

### 交易與取消

- 保留第一階段 snapshot 原子性：未 completed 的匯入內容不能查詢。
- 索引使用獨立狀態。Snapshot 已匯入但索引失敗時，第一階段檔案查詢仍可用，搜尋不得使用部分索引。
- 索引重試只清理本次索引版本的未完成衍生資料，不刪除有效 snapshot 或其他版本索引。
- 取消採合作式：開始前、檔案間與發布結果前檢查旗標；不使用強制終止 worker 作為主要機制。
- 取消與成功以條件更新決定先後，不能把已發布成功工作改為取消。取消後仍須遵守未完成資料不可見規則。

## 5. 資料模型草案

| 實體 | 主要資料與約束 |
| --- | --- |
| ImportJob | repo、完整 SHA、匯入／索引設定雜湊、snapshot ID、status、phase、進度、attempt、lease、執行 token、取消旗標、時間、安全錯誤摘要、來源 request ID；repo + SHA + 兩種設定雜湊唯一 |
| JobDispatch | job、attempt、待派送／已派送、派送次數與時間；job + attempt 唯一；只保存派送必要資訊 |
| SnapshotIndex | snapshot、索引版本／設定雜湊、狀態、統計、起訖時間；snapshot + 設定雜湊唯一 |
| IndexedFile | 索引、snapshot file、解析狀態、模組名稱、錯誤類型與位置；索引 + file 唯一 |
| Symbol | 索引、file、種類、名稱、qualified name、父符號、起訖行、欄位位置、async 標記；以 file、種類、qualified name、位置識別，不假設名稱唯一 |
| ImportReference | 索引、file、來源符號、import 模組／名稱、alias、相對層級與行號 |
| SymbolRelation | 來源符號、關係類型、可選目標符號、原始目標名稱、解析狀態；限定同索引 |

衍生資料需有外鍵及必要查詢索引；跨 snapshot／索引的符號關聯不可寫入。第一階段 snapshot 去重約束保留。

## 6. Python AST 索引規則

- 僅處理已 imported、具有文字內容的 `.py` 檔；讀取 snapshot 保存內容，不讀工作目錄、不 import 或執行受分析程式。
- 每檔建立模組符號；抽取 `ClassDef`、`FunctionDef`、`AsyncFunctionDef`、`Import`、`ImportFrom`，保留巢狀與父子關係。
- 行號為 1-based，起訖行皆包含；欄位沿用 AST 的 0-based UTF-8 byte offset，不能冒充字元位置。裝飾器另存起始行，避免與定義行混淆。
- 保存包含關係及 import 參照。類別基底只解析可確定的同檔符號；其他記為 unresolved 並保留名稱。
- 不以函式呼叫文字推斷完整呼叫圖，不解析動態 import、monkey patching 或執行期繫結；無法判斷時標示 unresolved。
- `SyntaxError` 按檔記錄類型與位置，不保存可能含秘密的原始程式片段；其他有效檔繼續索引。
- 記憶體耗盡、worker 中斷與資料庫錯誤屬工作失敗，不偽裝成單檔語法錯誤。
- 索引設定雜湊包含演算法版本與 Python parser 版本，避免 parser 改變卻誤用舊結果。
- 第二階段只承諾解析 worker 所用 Python 版本支援的語法；較新語法應明確記錄解析失敗。

## 7. 搜尋契約

- 三種搜尋均限定 URL 指定的 repo、snapshot，僅使用 completed 索引及 imported 檔案。
- `files`：路徑字面子字串；`symbols`：名稱／qualified name 字面子字串；`text`：內容逐行字面子字串。
- 初版區分大小寫，不提供正規表示式或隱含 SQL 萬用字元；`%`、`_`、反斜線須按字面處理。
- `q` 不可空白，長度上限建議 200 字元；`limit` 預設 20、最大 100，`offset` 非負。數值為擬議預設，非效能保證。
- 回應使用 `{items, limit, offset, has_more}`；排序為 path、起始行、穩定識別碼，不承諾相關性分數或未計算的 total。
- 檔案命中回傳 file ID、path；符號命中另含符號種類、名稱、起訖行；文字命中以一個命中行為單位，提供行號與截斷片段，不預設回傳全文。
- 所有命中均帶 repo ID、snapshot ID、完整 commit SHA；片段建議最多 300 字元，並標示是否截斷。
- 在 PostgreSQL 中先限定 snapshot 並分頁；不得把整個資料庫內容載入記憶體搜尋。設定查詢逾時，超時回明確錯誤，不回成功空列表。
- 初版採 PostgreSQL 字面比對，不提前加入向量擴充；實作時記錄查詢計畫與示範資料量，再判斷是否需要額外文字索引。

## 8. 依賴與執行環境

- 新增 Celery 及其 Redis transport 所需套件，用於第二階段背景工作；實作時選相容穩定版本並更新 `uv.lock`，草稿不預填未查證版本。
- Docker Compose 增加隔離的開發／測試 Redis；測試資源不得與開發共用佇列或資料庫。
- 本階段仍使用同步 SQLAlchemy；API 不在 `async def` 中執行同步 Git 或 DB 工作。
- Windows API 可在主機執行；Celery worker 的正式驗證採 Linux 容器或 WSL，不把 Windows `solo` 模式當成受支援的部署保證。
- Worker 容器需以唯讀方式掛載被匯入 repo；主機路徑與容器路徑需有明確設定映射，不能直接假設 `D:\repos\demo` 在容器中存在。
- Redis 僅做 broker；資料庫 volume 不是 Git repo volume。Dispatcher 與中斷復原程序須有文件化啟動方式。

## 9. 測試與驗收計畫

以下皆為**未完成／未執行**，不得以 Celery eager 模式取代真實 broker／worker 驗收。

| 情境 | 驗證方式與必要結果 |
| --- | --- |
| 背景匯入 | 真實 PostgreSQL + Redis + worker；阻擋 worker 時提交 API 仍取得 job ID，釋放後完成，不用任意耗時門檻當唯一證據 |
| 固定版本 | 提交後移動 HEAD、修改工作目錄，worker 仍匯入原完整 SHA |
| 重複提交／投遞 | 併發請求與重複訊息只產生一份有效 snapshot／索引；驗證資料庫唯一約束 |
| 派送故障 | broker 不可用、送出後 dispatcher 中斷、派送後訊息未被接收；恢復後可重送且不重複發布 |
| Worker 中斷 | lease 過期後可復原；舊執行 token 不能覆寫新 attempt，超過重試上限明確失敗 |
| 取消競態 | queued、running、完成邊界取消；狀態一致，未完成衍生資料不可搜尋 |
| AST 邊界 | 巢狀類別／函式、async、裝飾器、alias、相對 import、Unicode、重複名稱與 unresolved 關係 |
| 語法錯誤 | 單檔失敗有紀錄；其他檔仍可搜尋；不將基礎設施錯誤吞成解析成功 |
| 搜尋隔離 | 多 repo、同 repo 多 commit、不同索引版本不混用；非 completed 索引回 409 |
| 搜尋正確性 | 字面特殊字元、空查詢、大小寫、分頁、穩定排序、片段截斷、1-based 行號與非法 limit |
| 版本相容性 | 第一階段 API 回歸；同步匯入改非同步的契約測試與文件一致 |
| 持久化／migration | 從第一階段 schema 升級及從空 DB upgrade head；重啟後 job 與索引仍存在 |

驗收記錄須列出實際指令、日期、環境、通過／失敗／跳過，另執行 Ruff 與 mypy。未提供效能數據前不宣稱大規模搜尋效能。

## 10. 建議實作切片與確認事項

第一階段驗收後，依序：

1. Job／outbox schema、狀態機與 migration。
2. Redis、Celery、dispatcher、worker；串接既有匯入並驗證真正非同步流程。
3. 租約、重送、有限重試、取消及中斷復原。
4. AST 索引、解析錯誤與索引版本管理。
5. 符號查詢及三種 snapshot 限定搜尋。
6. PostgreSQL + Redis + worker 完整驗收；README、架構、roadmap 與第二階段學習文件。

使用者確認本草稿時，重點為：同步 API 改非同步的契約、合作式取消、outbox 與租約復原範圍、區分大小寫的字面搜尋。確認前不新增依賴、不改 API、不開始第二階段實作。
