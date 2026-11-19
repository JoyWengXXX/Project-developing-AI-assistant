import type { GitHubJobs } from '../useGitHubJobs'
import type { JobsOverview } from '../useJobsOverview'
import { RepoJobNotice } from './RepoJobNotice'

const NO_SNAPSHOTS: [] = []

/**
 * 「GitHub 註冊工作」：註冊成功前沒有 repo（沒有卡片），所以進行中與最近失敗的註冊工作列在這裡
 * （資料來自頁面共用的輪詢，重新整理頁面後仍看得到）：repo 名稱、階段、已耗時、已接收量、取消、重試／忽略。
 * 成功後新卡片出現在總覽；成功與否的說明（含「已自動匯入」）留在這裡，直到按「關閉」。
 * 沒有東西可顯示時不佔空間。
 */
export function GitHubRegistrations({ github, jobs }: { github: GitHubJobs; jobs: JobsOverview }) {
  const { registerActive, registerFailed, notices, dismissNotice, register } = github

  // 取消後的「重新註冊」：回傳是否排入了背景工作（已註冊或回退成同步則沒有）
  async function registerAgain(githubRepo: string): Promise<boolean> {
    const submit = await register(githubRepo)
    return submit.kind === 'job'
  }

  return (
    <>
      {notices.length > 0 && (
        <div className="panel registration-notices">
          {notices.map((n) => (
            <p key={n.jobId} className={n.tone === 'success' ? 'success' : 'warning'} role="status">
              {n.text}{' '}
              <button type="button" className="linklike" onClick={() => dismissNotice(n.jobId)}>
                關閉
              </button>
            </p>
          ))}
        </div>
      )}
      <RepoJobNotice
        title="GitHub 註冊工作"
        repoName=""
        jobs={{ active: registerActive, failed: registerFailed }}
        snapshots={NO_SNAPSHOTS}
        workerOffline={jobs.workerOffline}
        controls={jobs}
        registerAgain={registerAgain}
      />
    </>
  )
}
