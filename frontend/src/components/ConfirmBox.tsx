import { useEffect, useId, useRef, type ReactNode } from 'react'

interface Props {
  title: string
  children: ReactNode
  confirmLabel: string
  /** 動作進行中：停用兩個按鈕，Esc 也不會關閉。 */
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}

/**
 * 行內的確認對話（不是彈出視窗）：標題＋說明＋「確認／取消」。
 * 鍵盤：出現時焦點移到「取消」（預設走安全的選項）；Esc 取消；關閉（元件卸載）時焦點回到原本按的那顆按鈕
 * （若它已不在畫面上，例如該列已被移除，就不處理）。
 */
export function ConfirmBox({ title, children, confirmLabel, busy = false, onConfirm, onCancel }: Props) {
  const titleId = useId()
  const cancelRef = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null
    cancelRef.current?.focus()
    return () => {
      if (opener !== null && opener.isConnected) opener.focus()
    }
  }, [])

  return (
    <div
      className="remove-confirm"
      role="alertdialog"
      aria-labelledby={titleId}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && !busy) {
          e.stopPropagation()
          onCancel()
        }
      }}
    >
      <p id={titleId}>
        <strong>{title}</strong>
      </p>
      {children}
      <div className="action-row">
        <button type="button" className="danger-solid" disabled={busy} onClick={onConfirm}>
          {busy ? '處理中…' : confirmLabel}
        </button>
        <button type="button" className="secondary" ref={cancelRef} disabled={busy} onClick={onCancel}>
          取消
        </button>
      </div>
    </div>
  )
}
