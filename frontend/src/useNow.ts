import { useEffect, useState } from 'react'

/**
 * 每秒更新一次的「現在時間」（毫秒），供倒數與已耗時顯示使用。
 * stopAtMs：到了這個時間點就停止計時（倒數歸零後不必再每秒重繪）。
 * intervalMs：更新間隔；倒數與已耗時用預設的 1 秒，「3 分鐘前」這類相對時間用較長的間隔即可。
 */
export function useNow(stopAtMs: number | null = null, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = setInterval(() => {
      const t = Date.now()
      setNow(t)
      if (stopAtMs !== null && t >= stopAtMs) clearInterval(id)
    }, intervalMs)
    return () => clearInterval(id)
  }, [stopAtMs, intervalMs])
  return now
}
