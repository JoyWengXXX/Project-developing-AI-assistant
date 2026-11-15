# Embedding 模型另以唯讀掛載提供；執行時必須使用 network_mode: none。
ARG BASE_IMAGE=repopilot-worker:stage02-slice3
FROM python:3.12.16-slim-trixie AS runtime
ADD --checksum=sha256:9b6a8990abc16e97e327aee3ad8cc0de477b7ef7465b64a7c75a707886aefe73 \
    https://github.com/ggml-org/llama.cpp/releases/download/b12404/llama-b12404-bin-ubuntu-x64.tar.gz \
    /tmp/llama.tar.gz
RUN mkdir -p /opt/llama \
    && tar -xzf /tmp/llama.tar.gz --strip-components=1 -C /opt/llama \
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
