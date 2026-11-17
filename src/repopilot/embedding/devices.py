"""檢索索引工作可選的 embedding 運算裝置。

CPU 與 GPU 是兩個各自常駐的隔離 runtime（不同容器、不同私有 socket），執行同一份模型與同一個
執行環境，所以兩者的 `EmbeddingProfile`（設定識別）相同：切換裝置不使既有索引失效，兩種裝置的向量
可以互相搜尋與重用。GPU 的向量與 CPU 有微小數值差異（量測：餘弦相似度 ≥ 0.999998）。
"""

from typing import Literal

from repopilot.config import Settings
from repopilot.embedding import gateway_client
from repopilot.errors import EmbeddingDeviceUnavailableError

EmbeddingDevice = Literal["cpu", "gpu"]
DEVICES: tuple[EmbeddingDevice, ...] = ("cpu", "gpu")
DEFAULT_DEVICE: EmbeddingDevice = "cpu"


def device_of_params(params: dict[str, object]) -> EmbeddingDevice:
    """工作參數中的裝置；舊工作沒有這個欄位，視為 CPU。未知值是資料錯誤，不猜測。"""
    value = params.get("device", DEFAULT_DEVICE)
    if value == "cpu":
        return "cpu"
    if value == "gpu":
        return "gpu"
    raise EmbeddingDeviceUnavailableError("The job names an unknown embedding device.")


def ensure_available(settings: Settings, device: EmbeddingDevice) -> None:
    """提交工作前確認選取的裝置可用（CPU 沿用既有行為：不額外檢查）。"""
    if device == "cpu":
        return
    state = gateway_client.embedding_devices(settings)[device]
    if not state["configured"]:
        raise EmbeddingDeviceUnavailableError(
            "The GPU embedding runtime is not configured (see EMBEDDING_GPU_SOCKET_PATH)."
        )
    if not state["available"]:
        raise EmbeddingDeviceUnavailableError("The GPU embedding runtime is not responding.")
