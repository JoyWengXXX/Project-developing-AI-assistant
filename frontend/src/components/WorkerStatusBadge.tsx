import { formatTime } from '../format'
import { SERVICE_NAME, SERVICE_STATUS_BADGE, SERVICE_STATUS_LABEL } from '../jobFormat'
import type { ServiceState, ServiceStatus } from '../types'

const SERVICE_SYMBOL: Record<ServiceStatus, string> = { online: '●', stale: '▲', absent: '○' }

interface Props {
  service: 'dispatcher' | 'worker'
  state: ServiceState
}

/** dispatcher／worker 的狀態徽章（含最後心跳時間與在線實例數）：顏色＋符號＋文字並用，不單靠顏色分辨。 */
export function WorkerStatusBadge({ service, state }: Props) {
  return (
    <span className="service-state">
      {SERVICE_NAME[service]}：
      <span className={`badge ${SERVICE_STATUS_BADGE[state.status]}`}>
        <span aria-hidden="true">{SERVICE_SYMBOL[state.status]} </span>
        {SERVICE_STATUS_LABEL[state.status]}
      </span>
      <span className="muted">
        {' '}
        最後心跳：{formatTime(state.last_seen_at)}
        {state.status === 'online' && `（${state.instances_online} 個實例在線）`}
      </span>
    </span>
  )
}
