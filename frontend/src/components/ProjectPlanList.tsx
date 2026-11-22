import { useState } from 'react'
import { api } from '../api'
import { formatTime } from '../format'
import type { ProjectPlan, ProjectPlanList as PlanListData } from '../types'
import { ApiHint, ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { useLoad } from '../useLoad'
import { ConfirmBox } from './ConfirmBox'
import { PlanView } from './ProjectPlanPanel'

const ERROR_HINTS = {
  project_plan_locked: '這份計畫已確認（鎖定）；要刪除請在確認視窗中明確同意。',
  project_plan_not_found: '找不到這份計畫（可能已被刪除）。',
}

/**
 * 已保存的計畫：回頭查看與刪除。草稿直接刪除前先確認；已確認（鎖定）的計畫是第七階段的輸入，
 * 刪除時另外明確警告，後端也要求 `force=true`。只刪資料庫紀錄，不影響參考版本或任何檔案。
 */
export function ProjectPlanList({
  reloadKey, onDeleted,
}: {
  reloadKey: number
  onDeleted: (planId: number) => void
}) {
  const list = useLoad<PlanListData>('/api/v1/projects/plans?limit=50', reloadKey)
  const [viewing, setViewing] = useState<ProjectPlan | null>(null)
  const [pendingDelete, setPendingDelete] = useState<number | null>(null)
  const view = useAction()
  const remove = useAction()

  async function open(id: number) {
    await view.run(async () => {
      const response = await api<ProjectPlan>('GET', `/api/v1/projects/plans/${id}`)
      setViewing(response.data)
    })
  }

  async function doDelete(id: number, locked: boolean) {
    const ok = await remove.run(async () => {
      await api('DELETE', `/api/v1/projects/plans/${id}${locked ? '?force=true' : ''}`)
      onDeleted(id)
    })
    if (ok) {
      setPendingDelete(null)
      if (viewing?.id === id) setViewing(null)
    }
  }

  const items = list.data?.items ?? []
  return (
    <div>
      <ApiHint
        apis={[
          'GET /api/v1/projects/plans（新的在前）',
          'DELETE /api/v1/projects/plans/{id}（已確認的計畫須帶 force=true）',
        ]}
      />
      {list.loading && list.data === null && <p role="status">載入計畫中…</p>}
      <ErrorBox error={list.error} />
      {list.data !== null && items.length === 0 && <p className="empty">還沒有保存的計畫。</p>}
      {list.data !== null && list.data.total > items.length && (
        <p className="warning">只顯示最近 {items.length} 份，共 {list.data.total} 份。</p>
      )}
      <ul className="plan-list">
        {items.map((item) => (
          <li key={item.id}>
            <p>
              <span className={item.locked ? 'badge badge-completed' : 'badge badge-neutral'}>
                {item.locked ? '已確認（鎖定）' : '草稿'}
              </span>{' '}
              計畫 #{item.id}・{item.language} / {item.framework ?? '（無框架）'}・{item.file_count} 個檔案・
              {formatTime(item.created_at)}
            </p>
            <p className="muted">
              {item.reference === null
                ? '未使用參考 repo'
                : `參考 ${item.reference.repository_name}@${item.reference.commit_sha.slice(0, 12)}${item.reference.available ? '' : '（快照已移除）'}`}
              ・USD {item.cost_usd}
            </p>
            <p>{item.requirements_excerpt}</p>
            <div className="action-row">
              <button type="button" disabled={view.busy || remove.busy} onClick={() => void open(item.id)}>
                查看
              </button>
              <button
                type="button"
                className="danger"
                disabled={view.busy || remove.busy}
                onClick={() => {
                  remove.clear()
                  setPendingDelete(item.id)
                }}
              >
                刪除
              </button>
            </div>
            {pendingDelete === item.id && (
              <ConfirmBox
                title={item.locked ? `刪除已確認的計畫 #${item.id}？` : `刪除計畫 #${item.id}？`}
                confirmLabel="刪除計畫"
                busy={remove.busy}
                onConfirm={() => void doDelete(item.id, item.locked)}
                onCancel={() => setPendingDelete(null)}
              >
                <p>
                  只會刪除這份計畫的資料庫紀錄，不影響參考版本，也不會動到任何檔案；刪除後無法復原。
                  {item.locked && '這份計畫已確認，是第七階段（逐檔生成）的輸入，刪除後就不能再用它生成。'}
                </p>
              </ConfirmBox>
            )}
          </li>
        ))}
      </ul>
      <ErrorBox error={view.error} hints={ERROR_HINTS} />
      <ErrorBox error={remove.error} hints={ERROR_HINTS} />
      {viewing !== null && (
        <div className="plan-viewing">
          <h4>計畫 #{viewing.id}</h4>
          <PlanView plan={viewing} />
          <button type="button" onClick={() => setViewing(null)}>
            關閉
          </button>
        </div>
      )}
    </div>
  )
}
