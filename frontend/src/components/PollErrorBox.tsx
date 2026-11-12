import type { PollState } from '../usePolling'
import { ErrorBox } from '../ui'

/** 輪詢失敗的提示：沿用 ErrorBox，並說明畫面上的資料可能過期，以及下一次自動重試的時間。 */
export function PollErrorBox({ state }: { state: PollState<unknown> }) {
  if (state.error === null) return null
  return (
    <div>
      <ErrorBox error={state.error} />
      <p className="note">
        {state.data !== null && '下方顯示的是上一次成功取得的資料，可能已過期。'}
        {state.retryDelayMs !== null && `約 ${Math.round(state.retryDelayMs / 1000)} 秒後自動重試（失敗時會逐步拉長間隔）。`}
      </p>
    </div>
  )
}
