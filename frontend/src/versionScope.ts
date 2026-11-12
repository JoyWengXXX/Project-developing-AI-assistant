import type { RefsResponse } from './types'

/** 分支下拉「所有分支」選項的值（不是任何真正的 ref）。 */
export const ALL_BRANCHES = '__all__'

export interface VersionScope {
  /** 實際選用的下拉值：ALL_BRANCHES 或完整 ref 名稱。 */
  value: string
  /** 傳給 commits API 的 ref；null＝所有分支。 */
  ref: string | null
  /** 這個範圍的「最新 commit」：選到分支／標籤時是它指向的 commit；所有分支時是預設分支的最新 commit。 */
  tipSha: string | null
  /** 顯示用名稱（分支／標籤名稱，或「所有分支」）。 */
  label: string
}

/**
 * 依使用者的選擇決定目前的範圍。
 * choice 為 null（尚未選過）或指向已不存在的 ref（例如 fetch 後該分支消失）時，退回預設範圍：
 *   預設分支 → 與 head_sha 相同的分支（detached HEAD 時的近似）→ 所有分支。
 */
export function resolveScope(refs: RefsResponse, choice: string | null): VersionScope {
  const defaultBranch = refs.branches.find((b) => b.is_default) ?? null
  const allBranches: VersionScope = {
    value: ALL_BRANCHES,
    ref: null,
    tipSha: defaultBranch?.sha ?? refs.head_sha,
    label: '所有分支',
  }
  if (choice === ALL_BRANCHES) return allBranches

  const named = (c: string | null): VersionScope | null => {
    if (c === null) return null
    const branch = refs.branches.find((b) => b.ref === c)
    if (branch) return { value: branch.ref, ref: branch.ref, tipSha: branch.sha, label: branch.name }
    const tag = refs.tags.find((t) => t.ref === c)
    if (tag) return { value: tag.ref, ref: tag.ref, tipSha: tag.sha, label: tag.name }
    return null
  }

  const chosen = named(choice)
  if (chosen) return chosen
  const byDefault = named(refs.default_ref)
  if (byDefault) return byDefault
  if (refs.head_sha !== null) {
    const sameAsHead = refs.branches.find((b) => b.sha === refs.head_sha)
    if (sameAsHead) return named(sameAsHead.ref) ?? allBranches
  }
  return allBranches
}

/** repo 完全沒有 commit（空 repo）：沒有分支、沒有標籤、也沒有 HEAD。 */
export function isEmptyRepo(refs: RefsResponse): boolean {
  return refs.branches.length === 0 && refs.tags.length === 0 && refs.head_sha === null
}
