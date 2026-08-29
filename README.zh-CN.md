# ModelShelf

[English](README.md)

ModelShelf 是一个轻量级的中心化模型下载和不可变 artifact 存储服务。POSIX 文件系统以及每个
artifact 自带的 manifest 是事实来源；SQLite 仅用于构建可随时重建的目录索引。

v1 服务端支持 Hugging Face Hub、ModelScope CN、ModelScope AI、GitHub Releases、Kaggle
Models、Generic HTTP URL 和白名单内的服务端本地导入。独立的 NFS-Ganesha 容器通过
NFSv4.2 只读导出已完成的 artifact。客户端是面向 Linux/macOS、amd64/arm64 的独立 Go
二进制文件。

## 核心保证

- Hub 的 branch、tag 或 version 在发布前必须解析为不可变 commit/version。
- 服务端下载和客户端同步均先写入同一文件系统的 staging，再原子发布。
- 每个 artifact 都包含 `.modelshelf/manifest.json`，记录 source、requested/resolved
  revision、内容摘要，以及每个文件的路径、大小和 SHA-256。
- 已发布 artifact 不可修改。快速校验检查路径和大小；完整校验额外检查 SHA-256。
- 文件系统和 manifest 始终是事实来源；损坏或不兼容的 SQLite 索引会被保留并重建。
- ModelShelf 只负责 ingestion 和存储，不提供推理、RBAC、多租户或调度系统。

```text
管理 UI / Go CLI ── HTTP API ── 下载调度器 ── .staging ── artifacts/
                              └── 可重建 SQLite 索引              │
Go CLI ── NFS mount ───────────────────────────── NFS-Ganesha（只读）
```

## 使用 Docker Compose 快速启动

需要 Docker Compose、可运行 NFS-Ganesha 的 Linux Docker 宿主机，以及客户端可访问的 NFS
TCP 端口（默认 2049）。

1. 创建环境配置：

   ```bash
   cp .env.example .env
   ```

2. 生成 Web 登录密码 hash。以下两个 ModelShelf 命令都使用 Argon2id，会隐藏交互输入并要求
   二次确认：

   ```bash
   uv sync --package modelshelf-server
   uv run modelshelf-server hash-password

   # 独立客户端会生成相同格式的 PHC 字符串：
   modelshelf hash-password
   ```

   启用了 ARGON2ID 的 [OpenSSL 3.2+](https://docs.openssl.org/3.6/man7/EVP_KDF-ARGON2/) 也可以
   通过 `openssl kdf` 计算。由于 KDF 命令只返回派生 key，下面的示例会补齐 ModelShelf 所需
   PHC 格式中的参数和 salt：

   ```bash
   password="$(openssl rand -base64 24 | tr -d '\n')"
   salt_padded="$(openssl rand -base64 16 | tr -d '\n')"
   salt="$(printf '%s' "$salt_padded" | tr -d '=')"
   salt_hex="$(printf '%s' "$salt_padded" | openssl base64 -d -A | od -An -tx1 | tr -d ' \n')"
   digest="$(openssl kdf -binary -keylen 32 \
     -kdfopt "pass:$password" -kdfopt "hexsalt:$salt_hex" \
     -kdfopt iter:3 -kdfopt memcost:65536 -kdfopt lanes:4 ARGON2ID |
     openssl base64 -A | tr -d '=')"
   printf 'Admin password: %s\n' "$password"
   printf "MODELSHELF_ADMIN_PASSWORD_HASH='\$argon2id\$v=19\$m=65536,t=3,p=4\$%s\$%s'\n" \
     "$salt" "$digest"
   unset password salt_padded salt salt_hex digest
   ```

   Hash 中包含 `$`，因此写入 `.env` 时需要保留单引号。使用以下命令分别生成 session secret
   和 API token：

   ```bash
   openssl rand -hex 32 # MODELSHELF_SESSION_SECRET
   openssl rand -hex 32 # MODELSHELF_WRITE_TOKENS 中的一个 token
   ```

3. 在 `.env` 中设置客户端可访问的 NFS 地址和允许访问的私有网段，然后启动：

   ```bash
   docker compose up --build -d
   ```

管理 UI 位于 `http://localhost:8080`。Compose 只在服务端容器的 `/data` 挂载一个可写数据
volume。NFS-Ganesha 以只读方式挂载该 volume，只导出服务端的 `/data/artifacts`（在 NFS
容器中对应 `/export/artifacts`）；SQLite、任务、staging 和其他元数据均不在 NFS 命名空间内。

如果需要将元数据保存在 SSD、模型文件保存在另一个文件系统，可设置
`MODELSHELF_ARTIFACT_STORAGE_ROOT`。该目录包含 `.staging/` 和 `artifacts/`；两者位于同一个
mount 下才能保持原子发布。NFS 只应导出其中的 `artifacts/`。

### NFS 配置

| 配置                             | 含义                                                               |
| -------------------------------- | ------------------------------------------------------------------ |
| `MODELSHELF_NFS_PORT`            | Ganesha/系统 NFS 的真实监听端口；Compose 将容器内端口固定为 2049。 |
| `MODELSHELF_NFS_ADVERTISED_HOST` | 通过 `/api/v1/info` 下发的客户端可访问主机。                       |
| `MODELSHELF_NFS_ADVERTISED_PORT` | 可选的客户端端口；留空时复用监听端口。                             |
| `MODELSHELF_NFS_CLIENTS`         | 允许访问的 CIDR 列表，应保持为私有网段。                           |

在 Compose 中，显式设置的 advertised port 同时是映射到容器 2049 的宿主机端口。公网 CIDR
必须同时显式设置 `MODELSHELF_NFS_ALLOW_PUBLIC=true`。裸机部署使用
`MODELSHELF_STORAGE_ROOT` 指定数据目录，并让系统 NFS 服务只读导出其中的 `artifacts/`。
设置 `MODELSHELF_ARTIFACT_STORAGE_ROOT` 后，则应导出该目录下的 `artifacts/`。裸机运行
ModelScope 下载还需要安装 `git` 和 `git-lfs`；服务端镜像已经内置二者。

Artifact 页面和只读目录 API 默认公开。设置 `MODELSHELF_PUBLIC_ARTIFACTS=false` 后需要 Web
session 或 bearer token。任务创建和所有管理操作始终需要认证。

## 模型入库

创建任务页面会搜索模型 ID、获取可用 revision、执行带凭据的 preflight，并在允许提交前展示
resolved immutable revision、预计大小/文件数、元信息和 source 页面。手动输入 ID 或 revision
也执行相同校验。

下载任务展示已传输大小、瞬时/平均速度和 ETA，并支持暂停、恢复和取消。
`MODELSHELF_MAX_CONCURRENT_DOWNLOADS` 控制全局并发，
`MODELSHELF_MAX_CONCURRENT_DOWNLOADS_PER_SOURCE` 控制单个 source 的并发。
Hub 搜索、revision 查询和 preflight 默认最多等待 30 秒，超时返回 HTTP 504；可通过
`MODELSHELF_PROVIDER_METADATA_TIMEOUT_SECONDS` 调整。

`.env` 可设置各 source 的镜像和全局 HTTP(S) 代理。UI 会提示当前路由，并允许单个任务分别
绕过镜像或代理。ModelScope CN 和 AI 是两个独立 source，使用不同地址和 token，互相不是
mirror 或认证 fallback。

Generic HTTP 使用两阶段流程：先将 URL 下载到 staging 并推断元信息，再由管理员明确选择是否
解包并确认发布。URL 文本不作为 artifact identity，实际下载内容的摘要才是最终 identity。

导入已有服务端文件时，配置 `MODELSHELF_IMPORT_ROOTS` 后运行：

```bash
modelshelf-server import /srv/imports/Qwen-7B \
  --id team/Qwen-7B --name Qwen-7B --version v1
```

导入命令会经过 staging，拒绝软连接、特殊文件以及白名单外路径，并对相同内容去重。只有显式
指定 `--extract` 才会解包。

## 客户端 CLI

安装自部署服务端内置的匹配版本：

```bash
curl -fsSL 'https://modelshelf.example/install.sh' | sh
```

或安装 GitHub 最新版本：

```bash
curl -fsSL https://raw.githubusercontent.com/mr5/modelshelf/main/packages/client/install.sh | sh
```

安装脚本自动识别 Linux/macOS 和 amd64/arm64，并校验 SHA-256。默认安装到
`/usr/local/bin`；可以使用 `MODELSHELF_INSTALL_DIR` 修改。GitHub 安装脚本还支持
`MODELSHELF_VERSION=vX.Y.Z` 固定版本。

默认配置文件是 `~/.config/modelshelf/config.yml`，可以通过 `MODELSHELF_CONFIG` 修改：

```yaml
schemaVersion: 1
serverUrl: http://modelshelf.internal:8080
nfsLocalPath: /mnt/modelshelf
localBasePath: /var/lib/modelshelf
writeToken: optional-server-write-token
models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    revision: main # 可选，默认 main
  - alias: qwen-7b
    provider: modelscope-cn
    id: Qwen/Qwen2.5-7B-Instruct
    revision: master
    path: runtime/qwen-2.5-7b
```

`sync` 将不可变 revision 写入 `config.lock.yml`，不会修改用户的 desired-state 配置。普通
sync 保留现有 lock；`sync --update` 更新移动的 revision；`sync --frozen-lockfile` 在需要
修改 lock 时直接失败。Alias 是唯一引用，但多个 alias 可以共享同一份 canonical 模型。
`path` 只创建额外软连接，不改变模型文件的实际存储位置。

```text
<localBasePath>/
├── .modelshelf/layout.json
├── models/<source>/<model-id...>/
│   ├── <resolved-revision>/
│   └── <requested-revision> -> <resolved-revision>/
└── aliases/<alias> -> ../models/.../<resolved-revision>/
```

常用命令：

```bash
modelshelf mount
modelshelf add huggingface sentence-transformers/all-MiniLM-L6-v2 --alias mini-lm
modelshelf sync [alias] [--update | --frozen-lockfile]
modelshelf list
modelshelf search <query>
modelshelf status <alias>
modelshelf verify [--full] [--unexpected] <alias-or-path>
modelshelf remove [-y] <alias>
modelshelf tui
modelshelf unmount
modelshelf upgrade [--check] [--github]
```

`status` 的稳定退出码为：`0` 已满足 desired state、`2` 未就绪、`3` 已损坏、`4` 不可用或未
配置。Linux 的 `mount` 使用 systemd NFSv4.2 automount，需要 `nfs-utils`/`nfs-common`、
`systemd-escape` 和 sudo；macOS 使用 `mount_nfs`。

客户端独立构建和发行细节见 [packages/client/README.md](packages/client/README.md)。Web 集成
页面也会根据当前部署生成对应命令。

## 服务端存储与 schema

```text
data/
├── .modelshelf/storage.json
├── .modelshelf/catalog.sqlite3   # 可重建索引，不通过 NFS 导出
├── .modelshelf/jobs/<task-id>.json
├── .incoming/
├── .staging/
└── artifacts/<source>/<model-id...>/<resolved-revision>/
    ├── .modelshelf/manifest.json
    └── ...模型文件
```

只有 `artifacts/` 下包含有效 manifest 的目录才会被列出和导出。Hub ID 保留自然的
`vendor/model` 层级，仅对 path segment 中的不安全字符做百分号转义。各持久化格式独立进行
版本管理，详见 [schema migration 策略](docs/schema-migrations.md)。

## 本地开发

需要 Python 3.12+、Node 24、pnpm 和 Go 1.25+。

```bash
uv sync --all-packages --all-extras
pnpm install

# 服务端
MODELSHELF_ADMIN_PASSWORD_HASH='...' MODELSHELF_WRITE_TOKENS=dev-token \
  uv run modelshelf-server

# 另一个终端运行 UI
pnpm dev
```

验证命令：

```bash
uv run ruff check .
uv run mypy
uv run pytest -q
pnpm typecheck
pnpm build
(cd packages/client && go test -race ./... && go vet ./...)
```

其他文档：

- [验收记录](docs/acceptance.md)
- [Schema 与 migration 策略](docs/schema-migrations.md)
- [设计决策](docs/design-decisions.md)
