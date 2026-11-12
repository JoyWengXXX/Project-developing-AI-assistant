import { useState, type FormEvent } from 'react'
import { api, type ApiError } from '../api'
import { toApiError } from '../format'
import type { Repository } from '../types'
import { ErrorBox } from '../ui'

interface Props {
  /** 註冊成功（201 新建或 200 已存在）後通知父元件：重新載入列表；created 為 true（201）時由 App 自動匯入一次。 */
  onRegistered: (repo: Repository, created: boolean) => void
}

/** 「從 GitHub 註冊」表單。註冊是同步 clone：後端 clone 完成才會回應，所以要顯示載入狀態並避免重複送出。 */
export function GitHubRegisterForm({ onRegistered }: Props) {
  const [input, setInput] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const [message, setMessage] = useState<string | null>(null)

  async function register(e: FormEvent) {
    e.preventDefault()
    if (submitting) return
    setSubmitting(true)
    setError(null)
    setMessage(null)
    try {
      const result = await api<Repository>('POST', '/api/v1/repositories/github', {
        body: { repo: input.trim() },
      })
      const repo = result.data
      const created = result.status === 201
      setMessage(
        created
          ? `201 Created：已從 GitHub clone 並註冊新 repo（id ${repo.id}，${repo.github_owner}/${repo.github_name}），並自動匯入預設分支最新版本（只此一次，進度顯示在卡片上）。`
          : `200 OK：這個 GitHub repo 先前已註冊過，回傳既有的 repo（id ${repo.id}），沒有重新 clone，也不會匯入。`,
      )
      onRegistered(repo, created)
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div>
      <p className="api-hint">
        對應 API：<code>POST /api/v1/repositories/github</code>
      </p>
      <form onSubmit={register} className="form">
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
          後端會把 repo clone 到受管目錄 <code>MANAGED_REPOS_DIR</code>（需先在 .env 設定）。公開 repo 不需要
          token；私有 repo 請先在「GitHub 連線」摺疊區儲存 token。
        </p>
        <button type="submit" disabled={submitting || input.trim() === ''}>
          {submitting ? 'clone 中…' : '從 GitHub 註冊'}
        </button>
      </form>
      {submitting && (
        <p role="status" aria-busy="true">
          clone 中，可能需要數十秒到數分鐘…這是同步請求，請等待後端回應，不要重複送出。
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
