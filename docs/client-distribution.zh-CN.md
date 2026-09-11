# 多机客户端分发

主客户端 A 从中央 ModelShelf 同步模型，通过高速网卡向 B、C 提供 NFS 只读分发。
B、C 将文件复制到自己的 SSD，推理使用本地文件；`serverUrl` 始终负责模型目录和版本解析。
省略 `upstream` 就使用中央服务端下发的 NFS 地址。

## 配置主客户端 A

以下示例地址需要替换成实际地址。`192.168.100.1` 是 A 的高速网卡 IP，允许的网段是
`192.168.100.0/24`。同一批机器需要锁定相同的 artifact；仅在不同时间填写 `main` 不能保证版本相同。

`~/.config/modelshelf/config.yml`：

```yaml
schemaVersion: 3
serverUrl: http://modelshelf.internal:8080
localBasePath: /var/lib/modelshelf

distribution:
  enabled: true
  port: 2049 # 可选，省略时为 2049
  allow:
    - 192.168.100.0/24

models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    revision: main
```

A 需要 Linux、systemd、NFS-Ganesha VFS 和普通客户端的 NFS 挂载依赖。Ubuntu/DGX OS：

```bash
sudo apt-get install nfs-common nfs-ganesha nfs-ganesha-vfs iproute2
sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/modelshelf
modelshelf export plan
modelshelf export enable
modelshelf mount
modelshelf sync
modelshelf verify mini-lm --full
```

`export enable` 使用 sudo 安装 `/etc/ganesha/modelshelf.conf` 和独立的
`modelshelf-export.service`。它不安装软件包、不修改防火墙，也不修改现有 `/etc/exports`。
监听端口默认 TCP 2049，可通过 `distribution.port` 修改。如果端口已被其他服务占用，
命令会报错；可选择空闲端口或由管理员处理冲突。防火墙仅允许所需从客户端经高速接口访问选定端口。
CLI 没有内置 NFS server；NFS 协议由系统安装的 NFS-Ganesha 提供。

不需要在配置中填写导出路径或协议版本。远程路径固定为 `/modelshelf`，端口默认 2049，
客户端使用 NFSv4.1/TCP。配置中的 `allow` 必须是显式 CIDR 列表；单台主机可使用 `/32` 或 `/128`。
更新 allow 后重新执行 `modelshelf export enable`。

```bash
modelshelf export status
modelshelf export disable
```

`disable` 停止对外服务，保留模型和发布目录。将配置中的 `distribution.enabled` 改为 false
只停止后续发布，不会隐式停止已经运行的系统服务；要关闭分发还需执行 `export disable`。

## 配置从客户端 B、C

```yaml
schemaVersion: 3
serverUrl: http://modelshelf.internal:8080
localBasePath: /var/lib/modelshelf

upstream:
  host: 192.168.100.1
  port: 2049 # 可选，省略时为 2049

models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    revision: main
```

```bash
sudo apt-get install nfs-common
sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/modelshelf
modelshelf mount
modelshelf sync
modelshelf verify mini-lm --full
ip route get 192.168.100.1
findmnt /mnt/modelshelf
```

确认路由使用高速网卡。`host` 可填写 IP 或主机名，不包含协议、端口或路径。
端口单独使用可选的 `upstream.port`，默认 2049；直连时必须与主客户端的 `distribution.port` 一致。
两个端口字段都只接受 1–65535 的整数，显式填写 0 会报错。省略 upstream 时仍使用中央服务端下发的端口。
默认挂载点为 `/mnt/modelshelf`，原有 `nfsLocalPath` 字段仍可覆盖。
从客户端不需要运行 NFS 服务。升级后如果已有旧的 `hard` 挂载或挂载了其他上游，先使用旧配置
执行 `modelshelf unmount`，再用新配置执行 `modelshelf mount`；挂载来源错误时客户端会拒绝同步。

## 显式回退

不写 `fallback` 等价于 false。主客户端缺少 artifact、缺文件、权限不足、不可用或数据校验失败，
默认都报错，不访问中央 NFS。用户取消、本地写入失败和配置错误始终直接失败。
有限的 NFS 重试仍发生在同一个来源上，不属于回退。

只有明确配置以下内容才允许尝试中央 NFS：

```yaml
upstream:
  host: 192.168.100.1
  fallback: true
```

若主客户端使用自定义端口，在上面的 upstream 中也保留对应的 `port`。
修改后重新执行 `modelshelf mount`，它会额外准备 `/mnt/modelshelf-fallback`。
如果覆盖了 `nfsLocalPath`，回退挂载点是在该路径后追加 `-fallback`。
回退只处理上游读取、缺失、拒绝访问和完整性错误；不会因本地磁盘满、挂载来源错误或用户取消而触发。
回退沿用同一个已解析的 artifact，不重新解析移动分支。日志显示触发原因及回退来源；本地
`.modelshelf/sync.json` 的 `sourcePath` 记录实际复制来源。CLI 不会在 `sync` 中隐式执行 sudo 挂载。
`modelshelf unmount` 在 fallback 开启时移除两个挂载；关闭 fallback 前应先卸载旧配置。

中央 HTTP API 仍需可用；本次功能不提供离线元数据解析。本地文件已经完整且校验通过时，
不要求读取上游 NFS，也不会为了检查上游是否在线而重新传输。

## 只发布完整模型

```text
<localBasePath>/
├── models/                     # 本地模型及 .staging
├── aliases/                    # 本机软链接
└── .distribution/
    ├── staging/                # 发布准备目录，不导出
    └── published/              # NFS 仅导出这里
        └── <artifact.relativePath>/
            ├── 模型文件
            └── .modelshelf/manifest.json
```

`sync` 对新复制文件、恢复的文件和复用文件校验 SHA-256，再原子发布模型。启用分发后，
模型文件使用硬链接发布到独立目录，manifest 单独写入。配置、token、本机别名和同步临时文件不导出。
模型目录和分发目录必须处于同一文件系统；不支持硬链接时明确失败，不暗中复制另一份模型。
发布目录使用服务端只读规则和 root squash，远端无法修改模型。

不要原地修改本地不可变模型文件，因为硬链接共享相同内容。客户端修复使用新文件和目录原子替换。
删除本地模型不会撤销已经发布的副本；旧版本保留，避免其他机器的锁定版本突然不可用。
这也意味着只删除本地文件可能无法释放该模型占用的磁盘块。当前版本没有在线发布版本清理命令；
维护清理需先协调停止下游同步和关闭导出，不能在传输中随意删除已发布文件。

## 超时与兼容性

Linux CLI 创建的挂载使用 `ro,softerr,timeo=50,retrans=2`，故障时有限重试并返回错误，
而不是 `hard` 挂载的无限重试。该挂载用于将经过校验的文件复制到本地；推理请使用本地模型路径。
这些参数不是整个同步任务的严格超时，正在进行的内核读取仍需等待其重试结束。
macOS 使用系统 `mount_nfs` 和 `soft`；导出服务管理仅支持 Linux。

Linux 6.19+ 与部分 Ganesha 版本组合存在目录 delegation 的 `EREMOTEIO` 兼容性问题。
客户端只对这类只读元数据操作在同一个来源上重试一次；持续错误仍按配置报错或显式回退，
不会修改系统全局 NFS 参数。参见 [Ganesha issue #1385](https://github.com/nfs-ganesha/nfs-ganesha/issues/1385)。

200Gbps 是链路带宽，不保证文件复制达到 25GB/s；磁盘、哈希校验、CPU 和多个从客户端会影响吞吐。

## 测试

```bash
cd packages/client
go test ./...
# 回到仓库根目录
cd ../..
bash scripts/test_client_distribution.sh
```

集成脚本创建独立 Docker 网络、volume、两个 NFS-Ganesha exporter 和一个从客户端，实际执行
Linux CLI 的同步及 SHA-256 校验，并停止主客户端 exporter 验证断网行为。退出时自动清理测试资源。
默认使用主客户端自定义端口 12049，并保持中央服务端为 2049；可通过 `MODELSHELF_TEST_PEER_PORT` 覆盖。
需要 Docker 支持 privileged 容器和 NFS 客户端挂载；测试不会修改宿主机 NFS 服务或内核参数。
它验证协议及功能，不测量 DGX Spark 的 200G 性能，也不等同于实际 DGX OS 上的 systemd 服务安装验收。
