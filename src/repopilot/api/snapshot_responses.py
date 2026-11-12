"""快照回應的組裝（列表與詳情共用）：把 ORM 的 `Snapshot` 加上兩個衍生欄位。"""

from repopilot.api.schemas import SnapshotItemResponse, SnapshotResponse
from repopilot.db.models import Snapshot


def to_snapshot_item(
    snapshot: Snapshot, *, referenced: bool, current_profile_hash: str
) -> SnapshotItemResponse:
    return SnapshotItemResponse(
        **SnapshotResponse.model_validate(snapshot).model_dump(),
        is_referenced=referenced,
        is_stale_profile=snapshot.import_profile_hash != current_profile_hash,
    )
