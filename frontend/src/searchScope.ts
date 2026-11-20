import type { FileKind } from './types'

export const FILE_KIND_LABEL: Record<FileKind, string> = {
  source: '原始碼',
  test: '測試',
  docs: '文件',
  data: '資料檔',
  build: '建置產物',
}

/** 檢索索引只包含這三種（建置產物與資料檔不進索引），所以只提供這三個選項。 */
export const SELECTABLE_KINDS: readonly FileKind[] = ['source', 'test', 'docs']

export interface SearchScope {
  fileKinds: FileKind[]
  pathPrefix: string
}

/** 預設「原始碼＋測試」：文件常把語意搜尋的結果淹沒。 */
export const DEFAULT_SCOPE: SearchScope = { fileKinds: ['source', 'test'], pathPrefix: '' }

/** 送給後端的欄位；路徑前綴空白時不送。種類一個都沒勾時回 null（呼叫端應阻止送出）。 */
export function scopeBody(scope: SearchScope): { file_kinds: FileKind[]; path_prefix?: string } | null {
  if (scope.fileKinds.length === 0) return null
  return scope.pathPrefix === '' ? { file_kinds: scope.fileKinds } : { file_kinds: scope.fileKinds, path_prefix: scope.pathPrefix }
}
