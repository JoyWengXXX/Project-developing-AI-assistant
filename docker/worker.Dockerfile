# 第二階段切片 3：worker 與 dispatcher 共用的映像（同一份程式與 uv.lock，指令不同）。
# - 基底標籤固定為 2026-11-11 以 docker pull 確認存在的版本（Python 主次版本須與 .python-version 一致）。
# - 依賴以 `uv sync --frozen --no-dev` 安裝，版本完全由 uv.lock 決定（可重現）；uv 本身也固定版本。
# - 非 root（UID／GID 10001）；系統層 safe.directory 解決掛載檔案擁有者為 root 的 dubious ownership
#   （切片 1 實測）。加固（唯讀根檔案系統、無 capability）由 docker-compose.yml 設定。
# - 映像內沒有 .env、測試、前端、文件（見 .dockerignore）；秘密只經執行時的環境變數／env_file 傳入。
FROM python:3.12.16-slim-trixie AS builder

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
RUN pip install --no-cache-dir uv==0.10.7

WORKDIR /src
# 先只裝依賴（鎖定檔不變時這一層可以快取），再安裝專案本身（非 editable）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


FROM python:3.12.16-slim-trixie

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 固定 UID／GID；掛載點目錄只供 compose 掛載，內容不在映像內
RUN groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin worker \
    && mkdir -p /mnt/repos /mnt/managed

# 系統層設定：safe.directory 只在 protected 設定有效；以 /* 允許掛載根底下所有 repo
RUN git config --system --add safe.directory '/mnt/repos/*' \
    && git config --system --add safe.directory '/mnt/managed/*'

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1

USER 10001:10001
WORKDIR /home/worker
# 預設指令為 worker；dispatcher 在 compose 以 `python -m repopilot.jobs.dispatcher` 覆寫
CMD ["celery", "-A", "repopilot.jobs.worker", "worker", "--loglevel=INFO", "--without-gossip", "--without-mingle"]
