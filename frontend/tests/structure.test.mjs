import assert from 'node:assert/strict'
import { test } from 'node:test'
import { analyzersText } from '../src/structure.ts'

test('SQL analyzer is shown once alongside all other analyzers', () => {
  const tree = { tree_sitter: '1', grammar: '1', algorithm: 1 }
  const result = analyzersText({
    typescript: tree,
    javascript: tree,
    java: tree,
    go: tree,
    rust: tree,
    sql: { parser: '4.5.0', default_dialect: null, max_file_bytes: 20000 },
  })
  assert.equal((result.match(/SQL 抽取器/g) ?? []).length, 1)
  assert.match(result, /預設方言 未指定/)
})
