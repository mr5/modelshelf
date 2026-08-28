import { useEffect, useState } from "react";
import { api } from "../api.ts";
import type { ServerInfo } from "../types.ts";

const githubInstaller = "https://raw.githubusercontent.com/mr5/modelshelf/main/packages/client/install.sh";

function shellQuote(value: string) {
  return `'${value.replaceAll("'", `'"'"'`)}'`;
}

function nfsSource(host: string, exportPath: string) {
  const normalized = host.includes(":") && !host.startsWith("[") ? `[${host}]` : host;
  return `${normalized}:${exportPath}`;
}

function platformName(os: string, arch: string) {
  const operatingSystem = os === "darwin" ? "macOS" : os === "linux" ? "Linux" : os;
  const architecture = arch === "amd64" ? "x86_64" : arch === "arm64" ? "arm64 / Apple Silicon" : arch;
  return `${operatingSystem} · ${architecture}`;
}

function CopyBlock({ value, multiline = false }: { value: string; multiline?: boolean }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const [error, setError] = useState("");

  async function copy() {
    setError("");
    try {
      if (!navigator.clipboard) throw new Error("clipboard API unavailable");
      await navigator.clipboard.writeText(value);
      setState("copied");
    } catch (cause) {
      try {
        const input = document.createElement("textarea");
        input.value = value;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.append(input);
        input.select();
        const succeeded = document.execCommand("copy");
        input.remove();
        setState(succeeded ? "copied" : "failed");
        if (!succeeded) {
          setError(cause instanceof Error ? cause.message : String(cause));
        }
      } catch (fallbackCause) {
        setState("failed");
        const primary = cause instanceof Error ? cause.message : String(cause);
        const fallback = fallbackCause instanceof Error
          ? fallbackCause.message
          : String(fallbackCause);
        setError(`${primary}; fallback copy failed: ${fallback}`);
      }
    }
    window.setTimeout(() => {
      setState("idle");
      setError("");
    }, 1800);
  }

  return <div className={`copy-block${multiline ? " multiline" : ""}`}>
    <pre><code>{value}</code></pre>
    <button className="small" type="button" onClick={() => void copy()}>
      {state === "copied" ? "Copied" : state === "failed" ? "Copy failed" : "Copy"}
    </button>
    {error && <span className="lookup-error">Clipboard copy failed: {error}</span>}
  </div>;
}

export function IntegrationPage() {
  const [info, setInfo] = useState<ServerInfo | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    void api<ServerInfo>("/info")
      .then(setInfo)
      .catch((cause) => setError(cause instanceof Error ? cause.message : String(cause)));
  }, []);

  const installUrl = info?.client?.installUrl ?? `${window.location.origin}/install.sh`;
  const serverBase = installUrl.endsWith("/install.sh")
    ? installUrl.slice(0, -"/install.sh".length)
    : window.location.origin;
  const host = info?.nfs?.host ?? "modelshelf.internal";
  const port = info?.nfs?.port ?? 2049;
  const exportPath = info?.nfs?.exportPath ?? "/modelshelf";
  const source = nfsSource(host, exportPath);
  const linuxMount = `sudo mkdir -p /mnt/modelshelf\nsudo mount -t nfs4 -o ro,vers=4.2,port=${port},lookupcache=positive ${shellQuote(source)} /mnt/modelshelf`;
  const macMount = `sudo mkdir -p /Volumes/modelshelf\nsudo mount_nfs -o ro,vers=4,port=${port} ${shellQuote(source)} /Volumes/modelshelf`;
  const composeBind = `services:\n  inference:\n    image: your-inference-image\n    volumes:\n      - /mnt/modelshelf:/models:ro`;
  const composeNfs = `services:\n  inference:\n    image: your-inference-image\n    volumes:\n      - modelshelf:/models:ro\n\nvolumes:\n  modelshelf:\n    driver: local\n    driver_opts:\n      type: nfs\n      o: ${shellQuote(`addr=${host},nfsvers=4.2,port=${port},ro`)}\n      device: ${shellQuote(`:${exportPath}`)}`;
  const kubernetes = `apiVersion: v1\nkind: PersistentVolume\nmetadata:\n  name: modelshelf\nspec:\n  capacity:\n    storage: 1Pi\n  accessModes: [ReadOnlyMany]\n  mountOptions: [nfsvers=4.2, port=${port}, ro]\n  nfs:\n    server: ${host}\n    path: ${exportPath}\n    readOnly: true`;
  const config = `schemaVersion: 1\nserverUrl: ${serverBase}\nnfsLocalPath: /mnt/modelshelf\nlocalBasePath: /var/lib/modelshelf\n# Optional: enables sync to create a server download task when missing.\n# This is a CLI API token, not the Web UI password.\nwriteToken: replace-with-a-server-write-token\nmodels:\n  - alias: mini-lm\n    provider: huggingface\n    id: sentence-transformers/all-MiniLM-L6-v2\n    # Optional; defaults to main. May be a branch, tag, or commit.\n    revision: main\n  - alias: qwen-7b\n    provider: modelscope-cn\n    id: Qwen/Qwen2.5-7B-Instruct\n    revision: master\n    # Creates an additional symlink; model bytes remain in canonical storage.\n    path: runtime/qwen-2.5-7b`;
  const localLayout = `/var/lib/modelshelf/\n├── .modelshelf/layout.json\n├── models/<source>/<model-id...>/\n│   ├── <resolved-revision>/\n│   └── <requested-revision> -> <resolved-revision>/\n└── aliases/<alias> -> ../models/.../<resolved-revision>/`;
  const lockExample = `schemaVersion: 1\nmodels:\n  - alias: mini-lm\n    provider: huggingface\n    id: sentence-transformers/all-MiniLM-L6-v2\n    revision: main\n    resolvedRevision: 7dbbc90392e2f80f3d3c277d6e90027e55de9125\n    artifactId: huggingface:...\n    relativePath: huggingface/sentence-transformers/all-MiniLM-L6-v2/7dbbc90392e2f80f3d3c277d6e90027e55de9125\n    lockedAt: 2026-08-27T14:30:00Z`;

  return <div className="page integration-page">
    <div className="page-head">
      <div>
        <p className="eyebrow">Consume the shelf</p>
        <h1>Integration</h1>
        <p className="muted">Mount immutable artifacts directly, or reconcile selected models onto local storage with the client CLI.</p>
      </div>
    </div>
    {error && <div className="error-box">Could not load server integration details: {error}</div>}

    <div className="integration-choices">
      <a className="panel integration-choice" href="#nfs">
        <span className="choice-number">01</span>
        <div><h2>Direct NFS</h2><p>Read the central artifact filesystem without another copy.</p></div>
      </a>
      <a className="panel integration-choice" href="#client-cli">
        <span className="choice-number">02</span>
        <div><h2>Client CLI</h2><p>Declare desired models and sync verified copies to local NVMe.</p></div>
      </a>
    </div>

    <section className="integration-section" id="nfs">
      <div className="section-heading">
        <p className="eyebrow">Mode 01</p>
        <h2>Use the read-only NFS export directly</h2>
        <p className="muted">Best for browsing, shared development, or runtimes that tolerate network-backed reads. The export is immutable and must remain read-only on clients.</p>
      </div>
      {info && !info.nfs && <div className="warning"><strong>NFS discovery is not configured.</strong><span>The examples below use placeholders. Set <code>MODELSHELF_NFS_ADVERTISED_HOST</code> on the server to publish the real endpoint here and to the CLI.</span></div>}
      {info?.nfs && <div className="endpoint-strip"><span className="badge completed">Advertised NFSv{info.nfs.version}</span><code>{host}:{port} · {exportPath}</code></div>}

      <div className="doc-grid two-docs">
        <article className="panel doc-card">
          <h3>Linux mount</h3>
          <p>Install <code>nfs-common</code> (Debian/Ubuntu) or <code>nfs-utils</code> (RHEL/Fedora), then mount read-only.</p>
          <CopyBlock value={linuxMount} multiline />
        </article>
        <article className="panel doc-card">
          <h3>macOS mount</h3>
          <p>Uses the system NFS client. macOS negotiates NFSv4 with <code>vers=4</code>.</p>
          <CopyBlock value={macMount} multiline />
        </article>
      </div>

      <article className="panel doc-card">
        <h3>Persistent Linux automount</h3>
        <p>For production hosts, prefer a systemd automount so boot does not block when the network is unavailable. The client CLI’s <code>modelshelf mount</code> command creates matching <code>.mount</code> and <code>.automount</code> units automatically.</p>
        <CopyBlock value={`${source} /mnt/modelshelf nfs4 ro,vers=4.2,port=${port},lookupcache=positive,_netdev,nofail,x-systemd.automount 0 0`} />
        <p className="doc-note">The line above is also suitable for <code>/etc/fstab</code>. Unmount with <code>sudo umount /mnt/modelshelf</code>.</p>
      </article>

      <h3 className="subsection-title">Container environments</h3>
      <div className="doc-grid two-docs">
        <article className="panel doc-card">
          <h3>Docker Compose · host bind</h3>
          <p>Recommended when the host already mounts ModelShelf. The container receives an explicit read-only bind.</p>
          <CopyBlock value={composeBind} multiline />
        </article>
        <article className="panel doc-card">
          <h3>Docker Compose · managed NFS volume</h3>
          <p>The Docker daemon performs this mount, so the NFS host must be reachable from the daemon or Docker Desktop VM.</p>
          <CopyBlock value={composeNfs} multiline />
        </article>
      </div>
      <article className="panel doc-card">
        <h3>Kubernetes PersistentVolume</h3>
        <p>Bind this PV to a PVC and mount the claim with <code>readOnly: true</code> in inference pods.</p>
        <CopyBlock value={kubernetes} multiline />
      </article>
      <div className="integration-tip"><strong>Latency-sensitive inference</strong><span>Mount NFS as the shared source, then use the client CLI to reconcile selected artifacts to local NVMe. Inference runtimes do not need to depend on central NFS availability.</span></div>
    </section>

    <section className="integration-section" id="client-cli">
      <div className="section-heading">
        <p className="eyebrow">Mode 02</p>
        <h2>Sync desired models with the client CLI</h2>
        <p className="muted">The standalone Go binary supports Linux and macOS on x86_64 and arm64. It discovers NFS through this server, verifies manifests, and publishes local copies atomically.</p>
      </div>

      <article className="panel doc-card client-highlight">
        <div className="card-title-row"><div><h3>Automatic install from this server</h3><p>Gets the client version bundled with this ModelShelf server, detects OS and architecture, and verifies SHA-256 before installing.</p></div>{info?.client?.version && <span className="badge completed">v{info.client.version}</span>}</div>
        {info?.client?.available === false
          ? <div className="warning"><strong>No bundled client distribution.</strong><span>Build the server image with client packages, or use the GitHub installer below.</span></div>
          : <CopyBlock value={`curl -fsSL ${shellQuote(installUrl)} | sh`} />}
        <p className="doc-note">Installs to <code>/usr/local/bin/modelshelf</code>. Override the directory with <code>MODELSHELF_INSTALL_DIR="$HOME/.local/bin"</code>.</p>
      </article>

      <article className="panel doc-card">
        <h3>Install the latest release from GitHub</h3>
        <p>Use this when you deliberately want the newest published client instead of the server-matched version. Set <code>MODELSHELF_VERSION=vX.Y.Z</code> before the command to pin a release.</p>
        <CopyBlock value={`curl -fsSL ${githubInstaller} | sh`} />
      </article>

      <article className="panel doc-card">
        <h3>Manual download</h3>
        <p>Download the archive for the target machine, compare it with <code>checksums.txt</code>, extract it, then place <code>modelshelf</code> somewhere on <code>PATH</code>.</p>
        {info?.client?.available && <div className="platform-downloads">
          {info.client.platforms.map((platform) => <a className="platform-download" key={platform.filename} href={`${serverBase}/api/v1/client/${platform.filename}`}>
            <span>{platformName(platform.os, platform.arch)}</span><code>{platform.filename}</code>
          </a>)}
          <a className="platform-download checksum" href={`${serverBase}/api/v1/client/checksums.txt`}><span>SHA-256 checksums</span><code>checksums.txt</code></a>
        </div>}
        <CopyBlock value={`tar -xzf modelshelf_<os>_<arch>.tar.gz\nsudo install -m 0755 modelshelf /usr/local/bin/modelshelf\nmodelshelf --version`} multiline />
      </article>

      <div className="section-heading compact">
        <h2>Configure the client</h2>
        <p className="muted">The default file is <code>~/.config/modelshelf/config.yml</code>. Override it globally with <code>MODELSHELF_CONFIG</code> or per invocation with <code>--config</code>.</p>
      </div>
      <CopyBlock value={config} multiline />
      <div className="config-notes">
        <div><strong>schemaVersion</strong><span>Configuration schema understood by this CLI. A newer value requires upgrading the client.</span></div>
        <div><strong>serverUrl</strong><span>HTTP API used for discovery, search, task creation, and NFS endpoint lookup.</span></div>
        <div><strong>nfsLocalPath</strong><span>Absolute path where the read-only server export is mounted.</span></div>
        <div><strong>localBasePath</strong><span>Root containing canonical <code>models/</code> content and stable <code>aliases/</code> symlinks.</span></div>
        <div><strong>writeToken</strong><span>Optional server API token. Required only when <code>add</code> must create a missing download task or protected APIs are used.</span></div>
        <div><strong>models</strong><span>Desired-state list. Optional <code>revision</code> may be a branch, tag, or commit and defaults to <code>main</code>.</span></div>
        <div><strong>alias</strong><span>Optional globally unique CLI name. <code>aliases/&lt;alias&gt;</code> is an atomically managed symlink to canonical content.</span></div>
        <div><strong>path</strong><span>Optional additional symlink. Relative values stay below <code>localBasePath</code>; absolute values are accepted.</span></div>
      </div>
      <article className="panel doc-card">
        <h3>Local storage layout</h3>
        <p>Canonical model bytes and human-friendly references have separate roles below <code>localBasePath</code>. A branch or tag such as <code>main</code> is a sibling symlink to its locked immutable revision.</p>
        <CopyBlock value={localLayout} multiline />
        <p className="doc-note">The internal <code>models/.staging/</code> directory is temporary and is never a model reference path.</p>
      </article>
      <div className="warning"><strong>References do not duplicate model bytes.</strong><span>Multiple unique aliases may declare the same exact model and share one immutable directory. Requested-revision, alias, and path entries are symlinks only; <code>sync --update</code> atomically moves branch or tag links.</span></div>
      <article className="panel doc-card">
        <h3>Generated lock file</h3>
        <p><code>sync</code> writes resolved commits to <code>config.lock.yml</code> beside <code>config.yml</code>; it never writes observed state back into the user configuration. A custom <code>app.yml</code> uses <code>app.lock.yml</code>.</p>
        <CopyBlock value={lockExample} multiline />
        <p className="doc-note">Commit the lock with a shared config for reproducible deployments. The lock contains no API token.</p>
      </article>

      <div className="section-heading compact">
        <h2>Command reference</h2>
        <p className="muted">All commands accept <code>--config &lt;path&gt;</code>. Provider names are <code>huggingface</code>, <code>modelscope-cn</code>, <code>modelscope-ai</code>, <code>github-release</code>, <code>kaggle</code>, <code>http</code>, and <code>filesystem</code>.</p>
      </div>
      <div className="command-reference panel">
        <div><code>modelshelf mount</code><p>Discover the server’s NFS endpoint. On Linux, install and enable a systemd NFSv4.2 automount; on macOS, call <code>mount_nfs</code>.</p></div>
        <div><code>modelshelf unmount</code><p>Remove the configured mount. Linux also removes the generated systemd units.</p></div>
        <div><code>modelshelf add &lt;provider&gt; &lt;model-id&gt; [-r revision] [--alias alias] [--path path]</code><p>Add or update desired state and immediately sync. If the artifact is missing and <code>writeToken</code> exists, create a server download task.</p></div>
        <div><code>modelshelf remove &lt;alias&gt; [-y]</code><p>Remove desired state and its symlink references. Canonical files are offered for deletion only when no other lock entry references them; <code>-y</code> confirms that deletion.</p></div>
        <div><code>modelshelf search &lt;query&gt;</code><p>Search published server artifacts by model name or ID.</p></div>
        <div><code>modelshelf sync [alias] [--update] [--frozen-lockfile]</code><p>Reconcile the entire config into an atomic lock, then sync all or one selected model. <code>--update</code> refreshes moving revisions; frozen mode rejects any config/lock difference.</p></div>
        <div><code>modelshelf list</code><p>Show configured models, desired and observed revisions, local state, size, and last sync time.</p></div>
        <div><code>modelshelf status &lt;alias&gt;</code><p>Programmatic readiness check by alias: exit <code>0</code> ready, <code>2</code> not ready, <code>3</code> corrupt, <code>4</code> unavailable. Provider plus model ID remains supported.</p></div>
        <div><code>modelshelf verify &lt;model-path-or-alias&gt; [--unexpected]</code><p>Validate the manifest plus every expected path and size. <code>--unexpected</code> also reports files absent from the manifest.</p></div>
        <div><code>modelshelf verify --full &lt;model-path-or-alias&gt;</code><p>Run quick verification and additionally recompute every file’s SHA-256.</p></div>
        <div><code>modelshelf tui</code><p>Interactively browse and filter local desired state and server artifacts.</p></div>
        <div><code>modelshelf upgrade [--check]</code><p>Upgrade from the client distribution bundled with the configured server. The package checksum and reported binary version are verified before atomic replacement.</p></div>
        <div><code>modelshelf upgrade --github [--version vX.Y.Z]</code><p>Use the latest GitHub release, or a specific release, instead of the configured server. Development builds and downgrades require <code>--force</code>.</p></div>
        <div><code>modelshelf hash-password</code><p>Generate an Argon2id hash suitable for the server Web UI password setting. Use <code>--stdin</code> for automation.</p></div>
      </div>
      <CopyBlock value={`modelshelf mount\nmodelshelf add huggingface sentence-transformers/all-MiniLM-L6-v2 --revision main --alias mini-lm\nmodelshelf status mini-lm\nmodelshelf sync mini-lm\nmodelshelf verify --full mini-lm`} multiline />
    </section>
  </div>;
}
