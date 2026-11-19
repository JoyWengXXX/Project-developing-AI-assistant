# Embedding 模型另以唯讀掛載提供；執行時必須使用 network_mode: none。
# 使用 llama.cpp b12404 的 Ubuntu CUDA 12.8 發行檔（執行檔）加上對應的 CUDA 執行階段函式庫；
# 沒有 GPU（不加 GPU 預留）時走 CPU 路徑，該路徑實測比 ubuntu-x64 發行檔快約 2 倍（原因未查明）。
# 兩個檔案的 SHA-256 為 GitHub 對該 asset 公布的值，建置時以 ADD --checksum 核對。
ARG BASE_IMAGE=repopilot-worker:stage02-slice3
FROM python:3.12.16-slim-trixie AS runtime
ADD --checksum=sha256:42edd396109b273fa510c69205bb88fa8d912cc118b2b6d2e287c64f4cc8eb7c \
    https://github.com/ggml-org/llama.cpp/releases/download/b12404/llama-b12404-bin-ubuntu-cuda-12.8-x64.tar.gz \
    /tmp/llama.tar.gz
ADD --checksum=sha256:0ebfc4b2e66feaf89729e823e7848b9d73b0b71f6ff63ed6cca893779633c13f \
    https://github.com/ggml-org/llama.cpp/releases/download/b12404/cudart-llama-b12404-bin-ubuntu-cuda-12.8-x64.tar.gz \
    /tmp/cudart.tar.gz
RUN mkdir -p /opt/llama \
    && tar -xzf /tmp/llama.tar.gz --strip-components=1 -C /opt/llama \
    && tar -xzf /tmp/cudart.tar.gz --strip-components=1 -C /opt/llama \
    && rm -f /opt/llama/libggml-rpc.so /opt/llama/ggml-rpc-server

FROM ${BASE_IMAGE}
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=runtime /opt/llama /opt/llama
ENV LD_LIBRARY_PATH=/opt/llama
USER 10001:10001
CMD ["python", "-m", "repopilot.embedding.isolated_runtime"]
