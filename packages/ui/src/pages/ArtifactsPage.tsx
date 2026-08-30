import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { siGithub, siHuggingface, siKaggle, siModelscope } from "simple-icons";
import { api, formatBytes } from "../api.ts";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import { ArtifactFileTree } from "../components/ArtifactFileTree.tsx";
import { selectionSummary } from "../selection.ts";
import { sourceModelUrl } from "../source.ts";
import type { ArtifactDetail, ArtifactSummary, Page, Provider } from "../types.ts";

const pageSize = 48;

type SortOption = "created:desc" | "created:asc" | "name:asc" | "name:desc" | "size:desc" | "size:asc";

const providers: Array<{ value: Provider; label: string }> = [
  { value: "huggingface", label: "Hugging Face Hub" },
  { value: "modelscope-cn", label: "ModelScope CN" },
  { value: "modelscope-ai", label: "ModelScope AI" },
  { value: "github-release", label: "GitHub Releases" },
  { value: "kaggle", label: "Kaggle Models" },
  { value: "http", label: "Generic HTTP" },
  { value: "filesystem", label: "Filesystem import" },
];

function providerLabel(provider: Provider): string {
  return providers.find((item) => item.value === provider)?.label ?? provider;
}

const providerLogos: Partial<Record<Provider, { hex: string; path: string }>> = {
  huggingface: siHuggingface,
  "modelscope-cn": siModelscope,
  "modelscope-ai": siModelscope,
  "github-release": siGithub,
  kaggle: siKaggle,
};

function SourceLogo({ provider }: { provider: Provider }) {
  const logo = providerLogos[provider];
  if (logo) {
    return <span className="provider-logo" style={{ color: `#${logo.hex}` }} aria-hidden="true"><svg viewBox="0 0 24 24"><path fill="currentColor" d={logo.path} /></svg></span>;
  }
  if (provider === "http") {
    return <span className="provider-logo generic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="12" cy="12" r="8" /><path d="M4 12h16M12 4c2.2 2.3 3.3 5 3.3 8S14.2 17.7 12 20c-2.2-2.3-3.3-5-3.3-8S9.8 6.3 12 4Z" /></svg></span>;
  }
  return <span className="provider-logo generic" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M4 7.5h6l1.8 2H20v8.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7.5Z" /><path d="M4 10h16" /></svg></span>;
}

function artifactsPath(query: string, provider: Provider | "", sort: SortOption, offset: number) {
  const [sortBy, sortOrder] = sort.split(":") as ["created" | "name" | "size", "asc" | "desc"];
  const parameters = new URLSearchParams({
    limit: String(pageSize),
    offset: String(offset),
    sortBy,
    sortOrder,
  });
  if (query) parameters.set("q", query);
  if (provider) parameters.set("provider", provider);
  return `/artifacts/page?${parameters.toString()}`;
}

function artifactAlias(item: ArtifactSummary): string {
  return item.alias ?? (
    item.name
      .normalize("NFKD")
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") || "model"
  );
}

function shellArgument(value: string): string {
  return /^[a-zA-Z0-9_@%+=:,./-]+$/.test(value)
    ? value
    : `'${value.replaceAll("'", `'"'"'`)}'`;
}

function addCommand(item: ArtifactSummary): string {
  const command = [
    "modelshelf add",
    shellArgument(item.provider),
    shellArgument(item.sourceId),
    "--revision",
    shellArgument(item.resolvedRevision),
    "--artifact",
    shellArgument(item.alias ?? item.artifactId),
  ];
  command.push(
    "--alias",
    shellArgument(artifactAlias(item)),
  );
  return command.join(" ");
}

function modelConfig(item: ArtifactSummary): string {
  const quote = (value: string) => JSON.stringify(value);
  const lines = [
    `alias: ${quote(artifactAlias(item))}`,
    `provider: ${quote(item.provider)}`,
    `id: ${quote(item.sourceId)}`,
    `revision: ${quote(item.resolvedRevision)}`,
    `artifact: ${quote(item.alias ?? item.artifactId)}`,
  ];
  return [...lines, ""].join("\n");
}

export function ArtifactsPage({ canManage = false }: { canManage?: boolean }) {
  const navigate = useNavigate();
  const { artifactId } = useParams<{ artifactId: string }>();
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<Provider | "">("");
  const [sort, setSort] = useState<SortOption>("created:desc");
  const [items, setItems] = useState<ArtifactSummary[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<{ artifactId: string; kind: "command" | "config" }>();
  const [deletingArtifactId, setDeletingArtifactId] = useState<string>();
  const [artifactDetail, setArtifactDetail] = useState<ArtifactDetail>();
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [aliasInput, setAliasInput] = useState("");
  const [savingAlias, setSavingAlias] = useState(false);
  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError("");
      void api<Page<ArtifactSummary>>(artifactsPath(query, provider, sort, 0))
        .then((result) => {
          if (!active) return;
          setItems(result.items);
          setHasMore(result.hasMore);
        })
        .catch((cause) => {
          if (active) setError(cause instanceof Error ? cause.message : String(cause));
        })
        .finally(() => {
          if (active) setLoading(false);
        });
    }, 180);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [query, provider, sort]);

  useEffect(() => {
    if (!artifactId) {
      setArtifactDetail(undefined);
      setDetailError("");
      return;
    }
    let active = true;
    setArtifactDetail(undefined);
    setDetailLoading(true);
    setDetailError("");
    void api<ArtifactDetail>(`/artifacts/${encodeURIComponent(artifactId)}`)
      .then((result) => {
        if (!active) return;
        setArtifactDetail(result);
        setAliasInput(result.summary.alias ?? "");
      })
      .catch((cause) => {
        if (active) setDetailError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => { active = false; };
  }, [artifactId]);

  useEffect(() => {
    if (!artifactId) return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") navigate("/artifacts");
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [artifactId, navigate]);

  async function loadMore() {
    setLoading(true);
    setError("");
    try {
      const result = await api<Page<ArtifactSummary>>(artifactsPath(query, provider, sort, items.length));
      setItems((current) => [...current, ...result.items]);
      setHasMore(result.hasMore);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  async function copyText(item: ArtifactSummary, kind: "command" | "config", value: string) {
    try {
      if (!navigator.clipboard) throw new Error("clipboard API unavailable");
      await navigator.clipboard.writeText(value);
      setCopied({ artifactId: item.artifactId, kind });
      window.setTimeout(() => setCopied((current) => current?.artifactId === item.artifactId && current.kind === kind ? undefined : current), 1800);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function deleteArtifact(item: ArtifactSummary): Promise<boolean> {
    setDeletingArtifactId(item.artifactId);
    setError("");
    try {
      await api<void>(`/artifacts/${encodeURIComponent(item.artifactId)}`, { method: "DELETE" });
      setItems((current) => current.filter((candidate) => candidate.artifactId !== item.artifactId));
      if (artifactId === item.artifactId) navigate("/artifacts");
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setDeletingArtifactId(undefined);
    }
  }

  async function saveAlias() {
    if (!artifactDetail) return;
    setSavingAlias(true);
    setDetailError("");
    try {
      const alias = aliasInput.trim() || null;
      const summary = await api<ArtifactSummary>(`/artifacts/${encodeURIComponent(artifactDetail.summary.artifactId)}/alias`, {
        method: "PUT",
        body: JSON.stringify({ alias }),
      });
      setArtifactDetail((current) => current ? { ...current, summary } : current);
      setItems((current) => current.map((item) => item.artifactId === summary.artifactId ? summary : item));
      setAliasInput(summary.alias ?? "");
    } catch (cause) {
      setDetailError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSavingAlias(false);
    }
  }

  return <div className="page">
    <div className="page-head"><div><p className="eyebrow">Immutable storage</p><h1>Artifacts</h1><p className="muted">Only fully verified artifacts atomically added to this shelf appear here.</p></div></div>
    <div className="artifact-filters">
      <input className="search" placeholder="Search name, model ID or revision…" value={query} onChange={(event) => setQuery(event.target.value)} />
      <label><span>Source</span><select value={provider} onChange={(event) => setProvider(event.target.value as Provider | "")}>
        <option value="">All sources</option>
        {providers.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select></label>
      <label><span>Sort</span><select value={sort} onChange={(event) => setSort(event.target.value as SortOption)}>
        <option value="created:desc">Newest first</option>
        <option value="created:asc">Oldest first</option>
        <option value="name:asc">Name · A–Z</option>
        <option value="name:desc">Name · Z–A</option>
        <option value="size:desc">Size · largest first</option>
        <option value="size:asc">Size · smallest first</option>
      </select></label>
    </div>
    {error && <div className="error-box">{error}</div>}
    <div className="cards">{items.map((item) => {
      const sourceUrl = sourceModelUrl(item.provider, item.sourceId, item.resolvedRevision);
      const command = addCommand(item);
      const commandCopied = copied?.artifactId === item.artifactId && copied.kind === "command";
      const configCopied = copied?.artifactId === item.artifactId && copied.kind === "config";
      return <article className="artifact-card" key={item.artifactId}>
        <div className="artifact-title-row"><div className="artifact-card-heading"><h2>{item.alias ?? item.name}</h2>{item.alias && <span title={item.sourceId}>{item.name}</span>}</div><div className="artifact-title-controls"><span className="immutable-badge"><span aria-hidden="true">◆</span> Immutable</span>{canManage && <details className="artifact-overflow"><summary aria-label={`More actions for ${item.alias ?? item.name}`}>•••</summary><div className="artifact-menu" role="menu"><button type="button" className="ghost" onClick={() => navigate(`/artifacts/${encodeURIComponent(item.artifactId)}`)}>{item.alias ? "Change alias" : "Set alias"}</button><DeleteConfirm
          triggerLabel="Delete artifact"
          triggerClassName="danger-text artifact-delete-menu-item"
          title={`Delete ${item.name}?`}
          description={`Its manifest and all ${item.fileCount.toLocaleString()} files will be permanently removed. Logical size: ${formatBytes(item.totalSize)}. Because immutable files may be hardlinked across artifacts, the physical space released can be smaller.`}
          confirmLabel="Delete artifact"
          disabled={deletingArtifactId === item.artifactId}
          onConfirm={() => deleteArtifact(item)}
        /></div></details>}</div></div>
        {sourceUrl
          ? <a className="artifact-source" href={sourceUrl} target="_blank" rel="noreferrer">{item.sourceId} <span aria-hidden="true">↗</span></a>
          : <span className="artifact-source">{item.sourceId}</span>}
        <dl><div><dt>Source</dt><dd className="artifact-source-meta"><SourceLogo provider={item.provider} /><span>{providerLabel(item.provider)}</span></dd></div><div><dt>Resolved revision</dt><dd className="mono truncate" title={item.resolvedRevision}>{item.resolvedRevision}</dd></div><div><dt>Content</dt><dd>{item.fileCount.toLocaleString()} files · {formatBytes(item.totalSize)}</dd></div><div><dt>Selection</dt><dd className="truncate" title={selectionSummary(item.selectedPaths)}>{selectionSummary(item.selectedPaths)}</dd></div><div><dt>Added to shelf</dt><dd>{new Date(item.createdAt).toLocaleString()}</dd></div></dl>
        <div className="artifact-actions">
          <div className="artifact-command"><code title={command}>{command}</code><button className="ghost" aria-label={`Copy modelshelf add command for ${item.name}`} onClick={() => void copyText(item, "command", command)}>{commandCopied ? "Copied" : "Copy"}</button></div>
          <div className="artifact-secondary-actions"><button className="ghost artifact-action" onClick={() => navigate(`/artifacts/${encodeURIComponent(item.artifactId)}`)}>View details</button><button className="ghost artifact-action" onClick={() => void copyText(item, "config", modelConfig(item))} title="Copy this model entry, pinned to the resolved revision">{configCopied ? "Copied" : "Copy model config"}</button></div>
        </div>
      </article>;
    })}</div>
    {hasMore && <div className="load-more"><button onClick={() => void loadMore()} disabled={loading}>{loading ? "Loading…" : "Load more"}</button></div>}
    {!loading && items.length === 0 && <div className="empty"><h2>No matching artifacts</h2><p>Completed downloads will appear here.</p></div>}
    {artifactId && <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) navigate("/artifacts"); }}>
      <section className="task-modal artifact-detail-modal" role="dialog" aria-modal="true" aria-labelledby="artifact-modal-title">
        <header className="task-modal-header">
          <div className="task-modal-heading"><p className="eyebrow">Artifact details</p><h2 id="artifact-modal-title">{artifactDetail?.summary.alias ?? artifactDetail?.summary.name ?? "Artifact"}</h2>{artifactDetail?.summary.alias && <p className="artifact-modal-model-name">{artifactDetail.summary.name}</p>}</div>
          <div className="task-modal-header-actions"><span className="immutable-badge"><span aria-hidden="true">◆</span> Immutable</span><button className="modal-close" aria-label="Close artifact details" autoFocus onClick={() => navigate("/artifacts")}>×</button></div>
        </header>
        <div className="task-modal-body">
          {detailLoading && <div className="modal-loading">Loading artifact details…</div>}
          {detailError && <div className="error-box">{detailError}</div>}
          {artifactDetail && <>
            {canManage && <section className="artifact-alias-editor"><div><span>Alias</span><small>A unique, mutable label for this immutable artifact. It does not change its identity or storage path.</small></div><div><input value={aliasInput} maxLength={128} placeholder="Optional artifact alias" onChange={(event) => setAliasInput(event.target.value)} /><button type="button" disabled={savingAlias || aliasInput.trim() === (artifactDetail.summary.alias ?? "")} onClick={() => void saveAlias()}>{savingAlias ? "Saving…" : artifactDetail.summary.alias && !aliasInput.trim() ? "Remove alias" : "Save alias"}</button></div></section>}
            <div className="task-meta-grid">
              <div className="task-meta"><span>Source</span><strong>{sourceModelUrl(artifactDetail.summary.provider, artifactDetail.summary.sourceId, artifactDetail.summary.resolvedRevision)
                ? <a className="artifact-detail-source" href={sourceModelUrl(artifactDetail.summary.provider, artifactDetail.summary.sourceId, artifactDetail.summary.resolvedRevision)!} target="_blank" rel="noreferrer">{providerLabel(artifactDetail.summary.provider)} · {artifactDetail.summary.sourceId} ↗</a>
                : `${providerLabel(artifactDetail.summary.provider)} · ${artifactDetail.summary.sourceId}`}</strong></div>
              <div className="task-meta"><span>Selection</span><strong>{selectionSummary(artifactDetail.summary.selectedPaths)}</strong></div>
              <div className="task-meta"><span>Resolved revision</span><strong className="mono">{artifactDetail.summary.resolvedRevision}</strong></div>
              <div className="task-meta"><span>Content</span><strong>{artifactDetail.summary.fileCount.toLocaleString()} files · {formatBytes(artifactDetail.summary.totalSize)}</strong></div>
              <div className="task-meta"><span>Content SHA-256</span><strong className="mono">{artifactDetail.manifest.contentSha256}</strong></div>
              <div className="task-meta"><span>Added to shelf</span><strong>{new Date(artifactDetail.summary.createdAt).toLocaleString()}</strong></div>
              {artifactDetail.summary.selectionDigest && <div className="task-meta artifact-wide-meta"><span>Selection digest</span><strong className="mono">{artifactDetail.summary.selectionDigest}</strong></div>}
              <div className="task-meta artifact-wide-meta"><span>Storage path</span><strong className="mono">{artifactDetail.summary.relativePath}</strong></div>
            </div>
            <ArtifactFileTree files={artifactDetail.manifest.files} />
          </>}
        </div>
      </section>
    </div>}
  </div>;
}
