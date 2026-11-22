import { useState, type FormEvent } from 'react'
import { api } from '../api'
import type { ProjectInitResult } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'

const ERROR_HINTS = {
  project_path_invalid: '路徑必須是後端主機上的絕對路徑、不含 symlink，且位於 REPOSITORY_ROOTS 內。',
  project_dependencies_not_found: '專案根目錄找不到依賴檔（requirements.txt、package.json 等）。',
}

const CONFIDENCE_TEXT = { high: '高（語言、依賴檔與框架一致）', medium: '中（框架未能確認，請自行檢查）' }

/** 「匯入初始專案」：輸入本機路徑，後端驗證並偵測語言與框架（唯讀，不修改專案）。 */
export function ProjectInitForm({ onDetected }: { onDetected?: (result: ProjectInitResult) => void }) {
  const [path, setPath] = useState('')
  const [result, setResult] = useState<ProjectInitResult | null>(null)
  const action = useAction()

  async function submit(e: FormEvent) {
    e.preventDefault()
    setResult(null)
    await action.run(async () => {
      const response = await api<ProjectInitResult>('POST', '/api/v1/projects/init', {
        body: { project_path: path },
      })
      setResult(response.data)
      onDetected?.(response.data)
    })
  }

  const details = action.error?.details
  const suggestions = details && !Array.isArray(details) ? details.suggestions : undefined
  const detectedLanguage = details && !Array.isArray(details) ? details.detected_language : undefined

  return (
    <div>
      <ApiHint apis={['POST /api/v1/projects/init（唯讀偵測，不寫資料庫）']} />
      <form onSubmit={submit} className="form">
        <label>
          初始專案的本機絕對路徑
          <input
            type="text"
            value={path}
            onChange={(e) => setPath(e.target.value)}
            placeholder="例：D:\repos\my-new-project"
            required
          />
        </label>
        <p className="note">
          路徑必須位於後端 <code>REPOSITORY_ROOTS</code> 內；系統只讀取依賴檔與檔案名稱，不會修改專案或安裝依賴。
        </p>
        <button type="submit" disabled={action.busy || path.trim() === ''}>
          {action.busy ? '偵測中…' : '驗證並偵測'}
        </button>
      </form>
      <ErrorBox error={action.error} hints={ERROR_HINTS} />
      {suggestions && suggestions.length > 0 && (
        <div className="note" role="status">
          {detectedLanguage ? <p>偵測到的語言：{detectedLanguage}</p> : <p>沒有偵測到支援的語言。</p>}
          <ul>
            {suggestions.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </div>
      )}
      {result && <ProjectConfigSummary result={result} />}
    </div>
  )
}

function ProjectConfigSummary({ result }: { result: ProjectInitResult }) {
  const config = result.project_config
  const dependencies = Object.entries(config.dependencies)
  return (
    <div className="success" role="status">
      <p>
        偵測結果：<strong>{config.language}</strong> / <strong>{config.framework ?? '（未偵測到框架）'}</strong>
        ・信心：{CONFIDENCE_TEXT[result.confidence]}
      </p>
      <ul>
        <li>ORM：{config.orm ?? '—'}</li>
        <li>測試框架：{config.test_framework ?? '—'}</li>
        <li>資料庫：{config.database ?? '—'}</li>
        <li>依賴檔：{result.manifests.join('、')}</li>
        <li>
          原始碼檔案：
          {Object.entries(result.language_files)
            .map(([language, count]) => `${language} ${count}`)
            .join('、') || '無'}
        </li>
        <li>常見目錄：{result.directories.join('、') || '無'}</li>
        <li>根目錄：{config.root_path}</li>
      </ul>
      <details>
        <summary>相依概況（{dependencies.length} 項）</summary>
        <ul>
          {dependencies.map(([name, spec]) => (
            <li key={name}>
              {name} {spec}
            </li>
          ))}
        </ul>
      </details>
    </div>
  )
}
