import { useEffect, useState } from "react";
import { siGithub, siHuggingface, siKaggle, siModelscope } from "simple-icons";
import { api, formatBytes } from "../api.ts";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import { sourceModelUrl } from "../source.ts";
import type { ArtifactSummary, Provider } from "../types.ts";

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
    limit: String(pageSize + 1),
    offset: String(offset),
    sortBy,
    sortOrder,
  });
  if (query) parameters.set("q", query);
  if (provider) parameters.set("provider", provider);
  return `/artifacts?${parameters.toString()}`;
}

function artifactAlias(item: ArtifactSummary): string {
  return item.name
    .normalize("NFKD")
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .replace(/^-+|-+$/g, "") || "model";
}

function shellArgument(value: string): string {
  return /^[a-zA-Z0-9_@%+=:,./-]+$/.test(value)
    ? value
    : `'${value.replaceAll("'", `'"'"'`)}'`;
}

function addCommand(item: ArtifactSummary): string {
  return [
    "modelshelf add",
    shellArgument(item.provider),
    shellArgument(item.sourceId),
    "--revision",
    shellArgument(item.resolvedRevision),
    "--alias",
    shellArgument(artifactAlias(item)),
  ].join(" ");
}

function modelConfig(item: ArtifactSummary): string {
  const quote = (value: string) => JSON.stringify(value);
  return [
    `alias: ${quote(artifactAlias(item))}`,
    `provider: ${quote(item.provider)}`,
    `id: ${quote(item.sourceId)}`,
    `revision: ${quote(item.resolvedRevision)}`,
    "",
  ].join("\n");
}

export function ArtifactsPage({ canManage = false }: { canManage?: boolean }) {
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<Provider | "">("");
  const [sort, setSort] = useState<SortOption>("created:desc");
  const [items, setItems] = useState<ArtifactSummary[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [copied, setCopied] = useState<{ artifactId: string; kind: "command" | "config" }>();
  const [deletingArtifactId, setDeletingArtifactId] = useState<string>();
  useEffect(() => {
    let active = true;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError("");
      void api<ArtifactSummary[]>(artifactsPath(query, provider, sort, 0))
        .then((result) => {
          if (!active) return;
          setItems(result.slice(0, pageSize));
          setHasMore(result.length > pageSize);
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

  async function loadMore() {
    setLoading(true);
    setError("");
    try {
      const result = await api<ArtifactSummary[]>(artifactsPath(query, provider, sort, items.length));
      setItems((current) => [...current, ...result.slice(0, pageSize)]);
      setHasMore(result.length > pageSize);
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
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setDeletingArtifactId(undefined);
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
        <div className="artifact-title-row"><h2>{item.name}</h2><div className="artifact-title-controls"><span className="immutable-badge"><span aria-hidden="true">◆</span> Immutable</span>{canManage && <details className="artifact-overflow"><summary aria-label={`More actions for ${item.name}`}>•••</summary><div className="artifact-menu" role="menu"><DeleteConfirm
          triggerLabel="Delete artifact"
          triggerClassName="danger-text artifact-delete-menu-item"
          title={`Delete ${item.name}?`}
          description={`Its manifest and all ${item.fileCount.toLocaleString()} files (${formatBytes(item.totalSize)}) will be permanently removed from the shelf.`}
          confirmLabel="Delete artifact"
          disabled={deletingArtifactId === item.artifactId}
          onConfirm={() => deleteArtifact(item)}
        /></div></details>}</div></div>
        {sourceUrl
          ? <a className="artifact-source" href={sourceUrl} target="_blank" rel="noreferrer">{item.sourceId} <span aria-hidden="true">↗</span></a>
          : <span className="artifact-source">{item.sourceId}</span>}
        <dl><div><dt>Source</dt><dd className="artifact-source-meta"><SourceLogo provider={item.provider} /><span>{providerLabel(item.provider)}</span></dd></div><div><dt>Resolved revision</dt><dd className="mono truncate" title={item.resolvedRevision}>{item.resolvedRevision}</dd></div><div><dt>Content</dt><dd>{item.fileCount.toLocaleString()} files · {formatBytes(item.totalSize)}</dd></div><div><dt>Added to shelf</dt><dd>{new Date(item.createdAt).toLocaleString()}</dd></div></dl>
        <div className="artifact-actions">
          <div className="artifact-command"><code title={command}>{command}</code><button className="ghost" aria-label={`Copy modelshelf add command for ${item.name}`} onClick={() => void copyText(item, "command", command)}>{commandCopied ? "Copied" : "Copy"}</button></div>
          <button className="ghost artifact-action" onClick={() => void copyText(item, "config", modelConfig(item))} title="Copy this model entry, pinned to the resolved revision">{configCopied ? "Copied" : "Copy model config"}</button>
        </div>
      </article>;
    })}</div>
    {hasMore && <div className="load-more"><button onClick={() => void loadMore()} disabled={loading}>{loading ? "Loading…" : "Load more"}</button></div>}
    {!loading && items.length === 0 && <div className="empty"><h2>No matching artifacts</h2><p>Completed downloads will appear here.</p></div>}
  </div>;
}
