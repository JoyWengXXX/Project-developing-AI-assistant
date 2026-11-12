"""背景工作的程序入口（第二階段切片 3）：`dispatcher`、`worker`、`healthcheck`。

這些是獨立程序的進入點（Compose 的 `dispatcher`、`worker` 服務），不是 API 的一部分：
API 程序不匯入、也不連線這個套件。業務邏輯在 `services/`（`job_dispatch`、`job_runner`），
這裡只負責程序生命週期：設定、連線、訊號、迴圈。
"""
