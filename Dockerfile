FROM node:24-bookworm-slim AS ui
WORKDIR /src
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY packages/ui/package.json packages/ui/package.json
RUN pnpm install --frozen-lockfile
COPY packages/ui packages/ui
RUN pnpm --filter @modelshelf/ui build

FROM golang:1.25-bookworm AS client
ARG MODELSHELF_VERSION=0.1.0-beta.2
ARG MODELSHELF_COMMIT=unknown
WORKDIR /src
COPY packages/client/go.mod packages/client/go.sum packages/client/
RUN cd packages/client && go mod download
COPY packages/client packages/client
COPY scripts/package_client.sh scripts/package_client.sh
RUN OUTPUT_DIR=/out \
    CLIENT_VERSION="$MODELSHELF_VERSION" \
    CLIENT_COMMIT="$MODELSHELF_COMMIT" \
    sh scripts/package_client.sh

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MODELSHELF_STORAGE_ROOT=/data \
    MODELSHELF_UI_DIST=/app/ui \
    MODELSHELF_CLIENT_DIST=/app/client \
    MODELSCOPE_HOME=/data/.modelshelf/providers/modelscope/config \
    MODELSCOPE_CACHE=/data/.modelshelf/providers/modelscope/cache
WORKDIR /app
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates && \
    rm -rf /var/lib/apt/lists/*
COPY packages/core packages/core
COPY packages/server packages/server
RUN pip install --no-cache-dir ./packages/core && \
    pip install --no-cache-dir "./packages/server[providers]"
COPY --from=ui /src/packages/ui/dist /app/ui
COPY --from=client /out /app/client
RUN useradd --system --uid 10001 --home /nonexistent --shell /usr/sbin/nologin modelshelf && \
    mkdir -p /data && chown 10001:10001 /data
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["modelshelf-server"]
