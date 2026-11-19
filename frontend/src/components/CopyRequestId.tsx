import { useState } from 'react'

/** request_id 與「複製」按鈕：用它可以到後端日誌（單行 JSON，含 request_id）找到同一次請求。 */
export function CopyRequestId({ requestId }: { requestId: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  async function copy() {
    try {
      await navigator.clipboard.writeText(requestId)
      setState('copied')
    } catch {
      setState('failed') // 非安全環境或權限被拒：請使用者手動選取
    }
  }
  return (
    <>
      <code className="wrap">{requestId}</code>{' '}
      <button type="button" className="secondary small" onClick={() => void copy()}>
        複製
      </button>{' '}
      <span role="status" className="muted">
        {state === 'copied' && '已複製'}
        {state === 'failed' && '複製失敗，請手動選取'}
      </span>
    </>
  )
}
