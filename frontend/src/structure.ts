// 「程式結構」面板的顯示對照與純函式（不含 React；只匯入型別，方便用 Node 腳本單獨驗證）。
// 只負責「怎麼顯示」：解析規則、關係解析與錯誤判斷都在後端，前端不重做。

import type { ImportReference, IndexStatus, ParseStatus, SymbolRelation, SymbolSummary } from './types'

export const INDEX_STATUS_LABEL: Record<IndexStatus, string> = {
  none: '尚未建立索引',
  building: '建立中',
  completed: '已建立',
  failed: '建立失敗',
}

// 一律稱「部分解析」，不使用「已完整解析」（規格 10.4）
export const PARSE_STATUS_LABEL: Record<ParseStatus, string> = {
  parsed: '已解析',
  parsed_with_errors: '部分解析（有語法錯誤）',
  failed: '無法解析（整個檔案沒有符號）',
}

export const LANGUAGE_LABEL: Record<string, string> = {
  python: 'Python', csharp: 'C#', typescript: 'TypeScript', javascript: 'JavaScript', java: 'Java',
}

export const SYMBOL_KIND_LABEL: Record<string, string> = {
  module: '模組',
  namespace: 'namespace',
  class: '類別',
  struct: 'struct',
  record: 'record',
  interface: '介面',
  type_alias: '型別別名',
  enum: '列舉',
  enum_member: '列舉成員',
  delegate: '委派',
  function: '函式',
  method: '方法',
  constructor: '建構函式',
  property: '屬性',
  field: '欄位',
}

export const RELATION_TYPE_LABEL: Record<string, string> = {
  base_class: '基底類別',
  base_type: '基底型別',
  decorator: '裝飾器',
  attribute: '屬性（attribute）',
}

// ambiguous／unresolved 一律保留原始名稱，不猜測
export const RESOLUTION_LABEL: Record<SymbolRelation['resolution'], string> = {
  resolved_same_file: '同檔已解析',
  ambiguous: '同檔有多個同名，無法判斷',
  unresolved: '未解析',
}

/** 單頁載入符號的上限（後端上限 200）；子層超過時只顯示前面這些並說明。 */
export const SYMBOL_PAGE_LIMIT = 200

export function kindLabel(kind: string): string {
  return SYMBOL_KIND_LABEL[kind] ?? kind
}

/** 符號列的次要標記：async、條件式定義、語法錯誤附近（可能不完整）。 */
export function symbolFlags(symbol: Pick<SymbolSummary, 'is_async' | 'conditional' | 'has_errors' | 'modifiers'>): string[] {
  const flags: string[] = []
  if (symbol.is_async) flags.push('async')
  if (symbol.modifiers) flags.push(symbol.modifiers)
  if (symbol.conditional) flags.push('條件式定義')
  if (symbol.has_errors) flags.push('可能不完整')
  return flags
}

/** 行號範圍：「第 4 行」或「第 4–7 行」（1-based、含頭尾）。 */
export function lineRangeText(start: number, end: number): string {
  return start === end ? `第 ${start} 行` : `第 ${start}–${end} 行`
}

/** 第一個語法錯誤的位置；欄位是 0-based 的 UTF-8 位元組位移，顯示成 1-based 的欄號會誤導，所以標明單位。 */
export function errorPositionText(error: { line: number; column: number } | null): string {
  return error === null ? '位置不明' : `第 ${error.line} 行，位元組位移 ${error.column}`
}

/** import／using 一行的白話：`import os`、`from ..pkg import name as alias`、`using static System.Math`。 */
export function importText(ref: ImportReference): string {
  if (ref.kind === 'java_import') return `import ${ref.target}`
  if (ref.kind === 'java_static_import') return `import static ${ref.target}`
  if (ref.kind === 'script_import') {
    if (ref.imported_name === null) return `import '${ref.target}'`
    const alias = ref.alias === null ? '' : ` as ${ref.alias}`
    const name = ref.imported_name === 'default'
      ? ref.alias ?? 'default'
      : ref.imported_name === '*'
        ? `*${alias}`
        : `{ ${ref.imported_name}${alias} }`
    return `import ${name} from '${ref.target}'`
  }
  if (ref.kind === 'using') return `using ${ref.target}`
  if (ref.kind === 'using_static') return `using static ${ref.target}`
  if (ref.kind === 'global_using') return `global using ${ref.alias === null ? '' : `${ref.alias} = `}${ref.target}`
  if (ref.kind === 'using_alias') return `using ${ref.alias ?? ''} = ${ref.target}`
  const dots = '.'.repeat(ref.relative_level ?? 0)
  if (ref.kind === 'from_import') {
    const name = ref.imported_name === null ? '' : ref.imported_name
    const alias = ref.alias === null ? '' : ` as ${ref.alias}`
    return `from ${dots}${ref.target} import ${name}${alias}`
  }
  const alias = ref.alias === null ? '' : ` as ${ref.alias}`
  return `import ${ref.target}${alias}`
}

/** 抽取器版本的一行說明（analyzers 是不可信內容，只取預期欄位並當文字顯示）。 */
export function analyzersText(analyzers: Record<string, unknown>): string {
  const text = (value: unknown): string => (typeof value === 'string' || typeof value === 'number' ? String(value) : '?')
  const python = analyzers.python
  const csharp = analyzers.csharp
  const parts: string[] = []
  if (typeof python === 'object' && python !== null) {
    const info = python as Record<string, unknown>
    parts.push(`Python 抽取器（ast，Python ${text(info.python_major_minor)} 語法，規則版本 ${text(info.algorithm)}）`)
  }
  if (typeof csharp === 'object' && csharp !== null) {
    const info = csharp as Record<string, unknown>
    if (info.enabled === false) {
      parts.push('C# 未啟用')
    } else {
      const fields = info.include_fields === true ? '，含欄位' : '，不含欄位'
      parts.push(
        `C# 抽取器（tree-sitter ${text(info.tree_sitter)}，tree-sitter-c-sharp ${text(info.grammar)}，ABI ${text(info.abi_version)}，規則版本 ${text(info.algorithm)}${fields}）`,
      )
    }
  }
  for (const [language, label] of [['typescript', 'TypeScript'], ['javascript', 'JavaScript'], ['java', 'Java']] as const) {
    const analyzer = analyzers[language]
    if (typeof analyzer === 'object' && analyzer !== null) {
      const info = analyzer as Record<string, unknown>
      parts.push(`${label} 抽取器（tree-sitter ${text(info.tree_sitter)}，文法 ${text(info.grammar)}，規則版本 ${text(info.algorithm)}）`)
    }
  }
  return parts.length === 0 ? '沒有抽取器資訊' : parts.join('；')
}
