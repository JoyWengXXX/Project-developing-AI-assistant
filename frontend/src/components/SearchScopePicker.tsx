import { FILE_KIND_LABEL, SELECTABLE_KINDS, type SearchScope } from '../searchScope'
import type { FileKind } from '../types'

/** 搜尋範圍：檔案種類（依路徑判斷）與路徑前綴（區分大小寫、字面比對）。 */
export function SearchScopePicker({ scope, onChange }: { scope: SearchScope; onChange: (scope: SearchScope) => void }) {
  function toggle(kind: FileKind) {
    const fileKinds = scope.fileKinds.includes(kind) ? scope.fileKinds.filter((item) => item !== kind) : [...scope.fileKinds, kind]
    onChange({ ...scope, fileKinds: SELECTABLE_KINDS.filter((item) => fileKinds.includes(item)) })
  }
  return (
    <fieldset className="search-scope">
      <legend>搜尋範圍</legend>
      {SELECTABLE_KINDS.map((kind) => (
        <label key={kind}>
          <input type="checkbox" checked={scope.fileKinds.includes(kind)} onChange={() => toggle(kind)} /> {FILE_KIND_LABEL[kind]}
        </label>
      ))}{' '}
      <label>
        路徑前綴{' '}
        <input
          type="text"
          value={scope.pathPrefix}
          maxLength={1024}
          placeholder="例如 src/（可留空）"
          onChange={(event) => onChange({ ...scope, pathPrefix: event.target.value })}
        />
      </label>
      {scope.fileKinds.length === 0 && <p className="field-error">至少勾選一種檔案。</p>}
      <p className="note">種類只依路徑判斷（例如 tests/ 底下算測試、.md 算文件）；路徑前綴區分大小寫，% 與 _ 不是萬用字元。</p>
    </fieldset>
  )
}
