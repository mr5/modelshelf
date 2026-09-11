# Multi-machine client distribution

A publishing client downloads from central ModelShelf, then serves completed artifacts over its
high-speed network to followers. Followers copy files to their own SSDs; inference uses local files.
`serverUrl` remains the metadata authority. Omitting `upstream` uses the NFS endpoint advertised by
that server. See also the [Chinese deployment guide](client-distribution.zh-CN.md).

## Publishing client A (Linux)

```yaml
schemaVersion: 3
serverUrl: http://modelshelf.internal:8080
localBasePath: /var/lib/modelshelf

distribution:
  enabled: true
  port: 2049 # optional; defaults to 2049
  allow:
    - 192.168.100.0/24

models:
  - alias: mini-lm
    provider: huggingface
    id: sentence-transformers/all-MiniLM-L6-v2
    revision: main
```

Replace the addresses with your high-speed network. On Ubuntu/DGX OS:

```bash
sudo apt-get install nfs-common nfs-ganesha nfs-ganesha-vfs iproute2
sudo install -d -o "$(id -un)" -g "$(id -gn)" /var/lib/modelshelf
modelshelf export plan
modelshelf export enable
modelshelf mount
modelshelf sync
modelshelf verify mini-lm --full
```

`export enable` installs `/etc/ganesha/modelshelf.conf` and a dedicated
`modelshelf-export.service` using sudo. It does not install packages, change firewall rules, or
rewrite existing kernel NFS exports. `distribution.port` defaults to 2049. The selected port must
be available; a conflict fails explicitly. Choose another port or have an administrator resolve it.
The CLI does not embed an NFS server: system-installed NFS-Ganesha provides the protocol. Restrict firewall access to the
follower machines on the high-speed interface. `distribution.allow` requires explicit CIDRs;
individual hosts can use `/32` or `/128`. Run `export enable` again after changing the allow list.

The remote export is always `/modelshelf`; clients use NFSv4.1. No export path or protocol settings
are needed in YAML. Both `distribution.port` and `upstream.port` are optional, default to 2049, and
accept integers from 1 to 65535; explicit zero is invalid. For a direct connection they must match.
Omitting upstream still uses the central server-advertised port.

```bash
modelshelf export status
modelshelf export disable
```

Disabling stops the service and retains data. Setting `distribution.enabled: false` only stops
future publication; use `export disable` to stop an already running exporter.

## Followers B and C

Use the same base configuration and model declarations, omit `distribution`, and add:

```yaml
upstream:
  host: 192.168.100.1
  port: 2049 # optional; defaults to 2049
```

`host` is A's high-speed IP or hostname, without a port, scheme, or path. Install `nfs-common`,
prepare a writable `localBasePath`, then run:

```bash
modelshelf mount
modelshelf sync
modelshelf verify mini-lm --full
ip route get 192.168.100.1
findmnt /mnt/modelshelf
```

Followers do not run an NFS server. The default mount path is `/mnt/modelshelf`; the existing
`nfsLocalPath` override remains supported. Share immutable artifact resolutions/locks to ensure
identical versions; resolving `main` independently at different times does not guarantee this.
For an old `hard` mount or a different mounted source, run `modelshelf unmount` with the old
configuration before mounting the new one. The client refuses a mismatched source.

## Explicit fallback only

Omitting `fallback` means false: missing artifacts/files, unavailable peers, access denial and
integrity failures return an error without reading central NFS. To allow central fallback:

```yaml
upstream:
  host: 192.168.100.1
  fallback: true
```

Run `modelshelf mount` again to prepare the additional `/mnt/modelshelf-fallback` automount.
Retain `upstream.port` in this example if the publisher uses a custom port.
A custom `nfsLocalPath` gets the suffix `-fallback`. Sync does not implicitly run sudo to mount it.
Fallback retains the same resolved artifact, verifies all reused files, and records the reason and
actual source in output. `.modelshelf/sync.json` records the actual `sourcePath`.
Local write failures, invalid mount configuration and cancellation never trigger fallback.
`unmount` removes both mounts when fallback is enabled; unmount before disabling that setting.

Metadata API availability is still required. A fully verified local artifact does not need a live
NFS source. Bounded retries against the same source are not fallback.

## Publication and retention

Only `<localBasePath>/.distribution/published/` is exported. Publication staging lives outside it
at `.distribution/staging/`. Published artifacts contain manifest-listed files and
`.modelshelf/manifest.json`; no tokens, aliases or sync staging are exported.

Files are hardlinked from the verified local store; model storage and publication must share a
filesystem. Unsupported hardlinks fail explicitly rather than silently duplicating model bytes.
The manifest is written separately and publication uses atomic rename/exchange. Remote access is
read-only with root squash. Do not modify immutable model files in place: hardlinks share content.
Client repairs publish replacement files/directories instead.

Old published revisions remain available even after local model removal. Their hardlinks also
retain disk blocks. There is no online publication-prune command yet; coordinate maintenance,
stop follower transfers and disable the exporter before manually removing published revisions.

## Failure handling and compatibility

Linux managed mounts use `ro,softerr,timeo=50,retrans=2`, returning errors after finite NFS retries.
These copy-only mounts are paired with SHA-256 verification and atomic local publication. Use local
model paths for inference. Kernel retry timing is not a strict whole-sync deadline; cancellation
may wait for an in-flight NFS request. macOS uses `mount_nfs` with `soft`; exporter management is Linux-only.

Linux 6.19+ can receive EREMOTEIO from older Ganesha servers requesting directory delegation.
The client retries that read-only metadata operation once against the same source, without changing
global kernel parameters. Persistent errors still fail or follow explicitly enabled fallback.
See [Ganesha issue #1385](https://github.com/nfs-ganesha/nfs-ganesha/issues/1385).

200Gbps link capacity does not guarantee 25GB/s file throughput: storage, hashing, CPU and concurrent
followers can be bottlenecks.

## Validation

```bash
cd packages/client
go test ./...
cd ../..
bash scripts/test_client_distribution.sh
```

The integration script creates isolated Docker networking and storage, two Ganesha exporters and
one Linux follower. The peer uses custom port 12049 by default (override with
`MODELSHELF_TEST_PEER_PORT`), while central NFS uses 2049. It runs the real CLI, verifies checksums, tests server-side read-only access,
missing/corrupt files, and stops the peer to test outages with and without fallback. Resources are
removed on exit. Docker must support privileged containers and NFS mounts. The test does not alter
host NFS settings. It tests protocol/functionality, not DGX Spark bandwidth or native DGX OS systemd
installation.
