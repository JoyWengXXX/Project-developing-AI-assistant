import { useState, type FormEvent } from 'react'
import { type ApiError } from '../api'
import { toApiError } from '../format'
import type { RegisterSubmit } from '../githubActions'
import { ErrorBox } from '../ui'

interface Props {
  /** 送出註冊（useGitHubJobs.register）：worker 在線送背景工作，否則回退同步端點；失敗會拋出 ApiError。 */
  register: (input: string, onFallback: (reason: string) => void) => Promise<RegisterSubmit>
}

/** 送出結果的說明（留在表單下方）。 */
function resultMessage(submit: RegisterSubmit): string {
  switch (submit.kind) {
    case 'job':
      return `已排入背景註冊（工作 #${submit.job.id}）：查詢 GitHub、複製（clone）與寫入資料庫都在背景執行，進度與取消在上方的「GitHub 註冊工作」；完成後新卡片會出現在總覽，並自動匯入預設分支最新版本（只此一次）。可以離開或重新整理頁面，進度不會消失。`
    case 'existing':
      return `200 OK：這個 GitHub repo 先前已註冊過（repo #${submit.repositoryId}），沒有建立背景工作、沒有重新複製，也不會匯入。`
    case 'sync': {
      const repo = submit.repository
      return submit.created
        ? `201 Created：已從 GitHub clone 並註冊新 repo（id ${repo.id}，${repo.github_owner}/${repo.github_name}），並自動匯入預設分支最新版本（只此一次，進度顯示在卡片上）。`
        : `200 OK：這個 GitHub repo 先前已註冊過，回傳既有的 repo（id ${repo.id}），沒有重新 clone，也不會匯入。`
    }
  }
}

/**
 * 「從 GitHub 註冊」表單。背景執行器（worker 與 dispatcher）在線時送出背景工作：立刻回應，之後的進度顯示在上方的
 * 「GitHub 註冊工作」；否則回退到已 deprecated 的同步端點（請求會一直等到 clone 完成，所以要顯示等待狀態並避免重複送出）。
 */
export function GitHubRegisterForm({ register }: Props) {
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [syncReason, setSyncReason] = useState<string | null>(null) // 回退同步的原因（只有同步等待期間顯示）
  const [error, setError] = useState<ApiError | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setSyncReason(null)
    setError(null)
    setMessage(null)
    try {
      const result = await register(input.trim(), (reason) => setSyncReason(reason))
      setMessage(resultMessage(result))
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setSubmitting(false)
      setSyncReason(null)
    }
  }

  return (
    <div>
      <p className="api-hint">
        對應 API：<code>POST /api/v1/github/registration-jobs</code>
        <code>POST /api/v1/repositories/github（deprecated，worker 不可用時的同步回退）</code>
      </p>
      <form onSubmit={(e) => void submit(e)} className="form">
        <label>
          owner/repo 或 GitHub URL
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="例：octocat/Hello-World 或 https://github.com/octocat/Hello-World"
            spellCheck={false}
            autoComplete="off"
            required
          />
        </label>
        <p className="note">
          背景執行器會把 repo clone 到受管目錄 <code>MANAGED_REPOS_DIR</code>（需先在 .env 設定）。公開 repo 不需要
          token；私有 repo 請先在「GitHub 連線」摺疊區儲存 token。git clone 不提供百分比，進度只顯示階段、已耗時與已接收量。
        </p>
        <button type="submit" disabled={submitting || input.trim() === ''}>
          {submitting ? '送出中…' : '從 GitHub 註冊'}
        </button>
      </form>
      {submitting && (
        <p role="status" aria-busy="true">
          {syncReason === null
            ? '送出中…'
            : `${syncReason}（同步端點已 deprecated）：clone 中，可能需要數十秒到數分鐘…請求會一直等到後端完成，不要重複送出。`}
        </p>
      )}
      {message && (
        <p className="success" role="status">
          {message}
        </p>
      )}
      <ErrorBox error={error} />
    </div>
  )
}
