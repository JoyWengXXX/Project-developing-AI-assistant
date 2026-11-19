import { repoLabel } from '../format'
import type { Repository } from '../types'
import { ErrorBox } from '../ui'
import type { QaConsents } from '../useQaConsents'
import { QaConsentToggle } from './QaConsentToggle'

/** 設定面板裡的清單：每個 repo 一列同意狀態（與 repo 卡片上的是同一份資料、同一個元件）。 */
export function QaConsentList({ repos, consents }: { repos: Repository[] | null; consents: QaConsents }) {
  return (
    <>
      <p className="note">
        預設每個 repo 都是「未同意」。同意是逐 repo 的標記，也可以在總覽的 repo 卡片上操作。
      </p>
      {consents.loading && consents.byRepo === null && <p role="status">載入同意狀態中…</p>}
      <ErrorBox error={consents.error} />
      {repos === null && <p role="status">載入 repo 列表中…</p>}
      {repos !== null && repos.length === 0 && <p className="empty">尚未註冊任何 repo，沒有可同意的項目。</p>}
      {repos !== null && repos.length > 0 && (
        <ul className="qa-consent-list">
          {repos.map((repo) => (
            <li key={repo.id}>
              <h4>{repoLabel(repo)}</h4>
              <QaConsentToggle repo={repo} consents={consents} />
            </li>
          ))}
        </ul>
      )}
    </>
  )
}
