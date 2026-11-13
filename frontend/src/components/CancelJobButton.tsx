import { useRef, useState } from 'react'
import type { ApiError } from '../api'
import { toApiError } from '../format'
import { cancelJob } from '../jobActions'
import { cancelConsequence, cancelNoun, isCancelling } from '../jobFormat'
import type { Job } from '../types'
import { ErrorBox } from '../ui'
import { ConfirmBox } from './ConfirmBox'

interface Props {
  job: Job
  /** 按鈕文字（預設依工作種類：「取消匯入」「取消註冊」「取消更新」；卡片提示的一行內用「取消」）。 */
  label?: string
  small?: boolean
  /** 取消請求成功（或 409＝工作早已結束而重新讀取了結果）：交給呼叫端更新它持有的狀態。 */
  onResult: (job: Job) => void
}

/**
 * 「取消匯入／註冊／更新」：按下先出現行內確認（ConfirmBox：焦點移到「不取消」，Esc 關閉），確認後 POST /jobs/{id}/cancel。
 * 說明依工作種類（cancelConsequence）：例如註冊「會停止這次複製，已下載的暫存內容會被清除」。
 * 結果由 onResult 交回最新的 job：queued 會是 cancelled（200）；running 仍是 running 但 cancel_requested＝true（202），
 * 這時按鈕變成停用的「取消中…」，直到呼叫端輪詢到 cancelled。錯誤（404 等）顯示在按鈕下方，按鈕恢復可用。
 */
export function CancelJobButton({ job, label, small = false, onResult }: Props) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)
  const sending = useRef(false) // 同步擋住連按（state 要等下一次 render 才看得到）
  const cancelling = busy || isCancelling(job)
  const what = cancelNoun(job.kind)

  async function confirm() {
    if (sending.current) return
    sending.current = true
    setBusy(true)
    setError(null)
    try {
      const result = await cancelJob(job.id)
      setConfirming(false)
      onResult(result)
    } catch (e) {
      setError(toApiError(e))
      setConfirming(false)
    } finally {
      sending.current = false
      setBusy(false)
    }
  }

  return (
    <div className="cancel-job">
      {/* 取消中用 aria-disabled 而不是 disabled：按鈕仍可取得焦點（確認區塊關閉後焦點回到這裡，螢幕閱讀器會讀出「取消中…」），但按了沒有作用 */}
      <button
        type="button"
        className={small ? 'danger small' : 'danger'}
        aria-expanded={confirming}
        aria-disabled={cancelling}
        onClick={() => {
          if (!cancelling) setConfirming(true)
        }}
      >
        {cancelling ? '取消中…' : (label ?? `取消${what}`)}
      </button>
      {confirming && (
        <ConfirmBox
          title={`確定要取消這次${what}嗎？`}
          confirmLabel={`確定取消${what}`}
          dismissLabel="不取消"
          busy={busy}
          onConfirm={() => void confirm()}
          onCancel={() => setConfirming(false)}
        >
          <p>{cancelConsequence(job.kind)}</p>
        </ConfirmBox>
      )}
      <ErrorBox error={error} />
    </div>
  )
}
