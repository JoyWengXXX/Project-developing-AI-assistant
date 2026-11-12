import type { RefsResponse } from '../types'
import { ALL_BRANCHES } from '../versionScope'

interface Props {
  refs: RefsResponse
  /** 目前選用的值：ALL_BRANCHES 或完整 ref 名稱。 */
  value: string
  onChange: (value: string) => void
}

/** 分支／標籤下拉：「所有分支」＋「分支」群組＋「標籤」群組。選項的值是完整 ref 名稱。 */
export function BranchSelect({ refs, value, onChange }: Props) {
  return (
    <label>
      分支／標籤
      <select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value={ALL_BRANCHES}>所有分支</option>
        {refs.branches.length > 0 && (
          <optgroup label="分支">
            {refs.branches.map((b) => (
              <option key={b.ref} value={b.ref}>
                {b.name}
                {b.is_default ? '（預設）' : ''}
              </option>
            ))}
          </optgroup>
        )}
        {refs.tags.length > 0 && (
          <optgroup label="標籤">
            {refs.tags.map((t) => (
              <option key={t.ref} value={t.ref}>
                {t.name}
              </option>
            ))}
          </optgroup>
        )}
      </select>
    </label>
  )
}
