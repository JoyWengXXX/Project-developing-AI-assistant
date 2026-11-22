import { ProjectInitForm } from './ProjectInitForm'

/** 「新專案」摺疊區（預設收合）：以參考 repo 為依據建立全新專案的入口，目前第一步是匯入初始骨架。 */
export function NewProjectPanel() {
  return (
    <details className="panel advanced">
      <summary>新專案（匯入初始專案）</summary>
      <ProjectInitForm />
    </details>
  )
}
