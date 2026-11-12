import { useEffect, useRef, useState } from 'react'
import type { ApiError } from '../api'
import { commitShortSha } from '../format'
import { removeSnapshot } from '../snapshotRemoval'
import type { SnapshotItem } from '../types'
import { ErrorBox } from '../ui'
import { useAction } from '../useAction'
import { clearReference, setReference } from '../useReferences'
import { ConfirmBox } from './ConfirmBox'

interface Props {
  repoId: number
  sha: string
  /** 這個版本的快照（取自已匯入版本列表，才有快照編號與 is_referenced）；還沒取得為 null。 */
  snapshot: SnapshotItem | null
  /** 已匯入版本列表正在重新讀取：先停用動作，避免用過期的資料操作。 */
  listLoading: boolean
  listError: ApiError | null
  /** 重新讀取已匯入版本列表（取得快照編號失敗時的「重試」）。 */
  onReloadList: () => void
  /** 設定或取消參考成功（或失敗，資料可能已過期）：重新讀取。 */
  onChanged: () => void
  /** 已移除（或發現早已不存在）：父元件忘掉這個版本、重新讀取，並顯示 message。 */
  onRemoved: (sha: string, message: string) => void
}

/**
 * 已匯入版本的管理動作：設為參考版本／取消參考、移除此版本（需確認）。
 * 參考只是標記，尚未連到 Agent；移除只刪資料庫裡這個版本的紀錄，不動 Git repo。
 */
export function VersionActions({ repoId, sha, snapshot, listLoading, listError, onReloadList, onChanged, onRemoved }: Props) {
  const action = useAction()
  const [confirming, setConfirming] = useState(false)
  // 設定／取消參考後，原本按的那顆按鈕會換成另一顆：重新讀取完成、畫面換好之後把焦點移過去（鍵盤使用者不會掉到頁面最上方）
  const setButtonRef = useRef<HTMLButtonElement>(null)
  const cancelButtonRef = useRef<HTMLButtonElement>(null)
  const moveFocus = useRef(false)
  const referenced = snapshot?.is_referenced ?? false
  useEffect(() => {
    if (!moveFocus.current) return
    moveFocus.current = false
    ;(referenced ? cancelButtonRef : setButtonRef).current?.focus()
  }, [referenced])
  if (snapshot === null) {
    return (
      <div className="version-actions">
        {listLoading && <p className="note">正在取得這個版本的快照編號…</p>}
        <ErrorBox error={listError} />
        {!listLoading && (
          <>
            <p className="note">
              找不到這個版本對應的快照（例如剛被移除，或讀取列表失敗），所以暫時不能設為參考版本或移除。
            </p>
            <button type="button" className="secondary" onClick={onReloadList}>
              重新讀取已匯入的版本
            </button>
          </>
        )}
      </div>
    )
  }

  const id = snapshot.id
  const disabled = action.busy || listLoading

  async function setAsReference() {
    moveFocus.current = true
    const ok = await action.run(async () => {
      await setReference(repoId, id)
      return '已設為參考版本（目前只是標記，尚未連到 Agent）。'
    })
    if (!ok) moveFocus.current = false
    onChanged()
  }

  async function cancelReference() {
    moveFocus.current = true
    const ok = await action.run(async () => {
      await clearReference(repoId)
      return '已取消參考版本。'
    })
    if (!ok) moveFocus.current = false
    onChanged()
  }

  async function remove() {
    await action.run(async () => {
      const outcome = await removeSnapshot(id, referenced) // 參考版本：使用者已在確認對話看過說明，帶 force=true
      if (outcome.kind === 'removed') {
        const r = outcome.result
        onRemoved(sha, `已移除快照 #${r.snapshot_id}（刪除 ${r.deleted_files.toLocaleString()} 個檔案${r.was_referenced ? '，並一併取消參考' : ''}）。`)
        return
      }
      if (outcome.kind === 'gone') {
        onRemoved(sha, `快照 #${id} 已不存在，列表已重新整理。`)
        return
      }
      if (outcome.kind === 'referenced') onChanged() // 剛被設為參考版本：重新讀取，下次確認會顯示「同時取消參考」
      throw outcome.error // busy／其他錯誤：顯示在下方，不重試
    })
    setConfirming(false)
  }

  return (
    <div className="version-actions">
      <div className="action-row">
        {referenced ? (
          <>
            <span className="badge badge-completed">
              <span aria-hidden="true">★ </span>目前的參考版本
            </span>
            <button type="button" className="secondary" ref={cancelButtonRef} disabled={disabled} onClick={() => void cancelReference()}>
              取消參考
            </button>
          </>
        ) : (
          <button type="button" ref={setButtonRef} disabled={disabled} onClick={() => void setAsReference()}>
            設為參考版本
          </button>
        )}
        <button type="button" className="danger" disabled={disabled} aria-expanded={confirming} onClick={() => setConfirming(true)}>
          移除此版本
        </button>
        <span className="note">參考版本只是標記，尚未連到 Agent；每個 repo 最多一個，設定新的會取代舊的。</span>
      </div>

      {confirming && (
        <ConfirmBox
          title={`確定要移除版本 ${commitShortSha(sha)}（快照 #${id}）嗎？`}
          confirmLabel={referenced ? '移除並取消參考' : '確定移除'}
          busy={action.busy}
          onConfirm={() => void remove()}
          onCancel={() => setConfirming(false)}
        >
          <p>只會刪除資料庫中這個版本的紀錄，不會動你的 Git repo；之後可以再按「匯入此版本」重新匯入。</p>
          {referenced && <p>這個版本是目前的參考版本，移除會同時取消參考。</p>}
        </ConfirmBox>
      )}

      {action.notice !== null && (
        <p className="success" role="status">
          {action.notice}
        </p>
      )}
      <ErrorBox error={action.error} />
    </div>
  )
}
