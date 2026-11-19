import { useEffect, type RefObject } from 'react'

/**
 * trigger 變成 true 的那一刻，若鍵盤焦點已掉到頁面最上層（例如原本的「取消」按鈕被換成別的畫面），
 * 把焦點移到 ref 指的區塊（不捲動頁面）；焦點還在別的控制項上就不動它。
 */
export function useFocusIfLost(ref: RefObject<HTMLElement | null>, trigger: boolean): void {
  useEffect(() => {
    if (!trigger) return
    const active = document.activeElement
    if (active === null || active === document.body) ref.current?.focus({ preventScroll: true })
  }, [ref, trigger])
}
