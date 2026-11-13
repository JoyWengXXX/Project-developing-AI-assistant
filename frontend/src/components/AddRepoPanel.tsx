import { useState, type FormEvent } from 'react'
import { api, type ApiError } from '../api'
import { toApiError } from '../format'
import type { Repository } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import type { GitHubJobs } from '../useGitHubJobs'
import { GitHubRegisterForm } from './GitHubRegisterForm'

interface Props {
  /** GitHub 註冊（背景工作或同步回退）由 App 的 useGitHubJobs 送出與追蹤。 */
  github: GitHubJobs
  /** 本機路徑註冊成功（201 新建或 200 已存在）後通知父元件：重新載入列表；created 為 true（201）時由 App 自動匯入一次。 */
  onRegistered: (repoId: number, created: boolean) => void
}

/**
 * 「新增 repo」摺疊區（預設收合）：從 GitHub 或本機路徑註冊。成功訊息留在表單下方，新卡片出現在總覽。
 * GitHub 註冊是背景工作：進行中與失敗的註冊列在上方的「GitHub 註冊工作」區（不在這個摺疊區內）。
 */
export function AddRepoPanel({ github, onRegistered }: Props) {
  const [tab, setTab] = useState<'github' | 'local'>('github')

  const [path, setPath] = useState('')
  const [name, setName] = useState('')
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
      // name 為空就不送（後端會自己決定預設名稱）
      const body = name.trim() === '' ? { path } : { path, name }
      const result = await api<Repository>('POST', '/api/v1/repositories', { body })
      const created = result.status === 201
      setMessage(
        created
          ? `201 Created：已註冊新 repo（id ${result.data.id}），卡片已加入總覽，並自動匯入預設分支最新版本（只此一次，進度顯示在卡片上）。`
          : `200 OK：這個路徑先前已註冊過，回傳既有的 repo（id ${result.data.id}），不會匯入。`,
      )
      onRegistered(result.data.id, created)
    } catch (err) {
      setError(toApiError(err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <details className="panel advanced">
      <summary>新增 repo（從 GitHub 或本機路徑註冊）</summary>
      <ApiHint
        apis={[
          'POST /api/v1/github/registration-jobs（GitHub：背景工作；已註冊回 200，不建立工作）',
          'POST /api/v1/repositories/github（deprecated：worker 不可用時的同步回退）',
          'POST /api/v1/repositories（本機路徑）',
          'POST /api/v1/repositories/{id}/import-jobs（註冊成功且是新建時自動提交一次，revision=HEAD；worker 不可用時回退同步 /imports）',
        ]}
      />
      <div role="tablist" aria-label="註冊方式" className="tabs">
        <button
          type="button"
          role="tab"
          id="tab-github"
          aria-selected={tab === 'github'}
          aria-controls="tabpanel-github"
          className="tab"
          onClick={() => setTab('github')}
        >
          從 GitHub 註冊
        </button>
        <button
          type="button"
          role="tab"
          id="tab-local"
          aria-selected={tab === 'local'}
          aria-controls="tabpanel-local"
          className="tab"
          onClick={() => setTab('local')}
        >
          本機路徑註冊
        </button>
      </div>

      {/* 兩個分頁都保持掛載（只用 hidden 隱藏），切換分頁不會丟失輸入或進行中的請求狀態 */}
      <div role="tabpanel" id="tabpanel-github" aria-labelledby="tab-github" hidden={tab !== 'github'}>
        <GitHubRegisterForm register={github.register} />
      </div>

      <div role="tabpanel" id="tabpanel-local" aria-labelledby="tab-local" hidden={tab !== 'local'}>
        <form onSubmit={register} className="form">
          <label>
            本機 repo 絕對路徑
            <input
              type="text"
              value={path}
              onChange={(e) => setPath(e.target.value)}
              placeholder="例：D:\repos\demo"
              required
            />
          </label>
          <label>
            名稱（選填）
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          <p className="note">
            路徑必須位於後端 <code>REPOSITORY_ROOTS</code> 內，且是 Git repo 根目錄；由後端主機直接讀取。
          </p>
          <button type="submit" disabled={submitting || path.trim() === ''}>
            {submitting ? '註冊中…' : '註冊本機 repo'}
          </button>
        </form>
        {message && (
          <p className="success" role="status">
            {message}
          </p>
        )}
        <ErrorBox error={error} />
      </div>
    </details>
  )
}
