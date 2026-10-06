import {
  Copy,
  ExternalLink,
  Eye,
  Gem,
  HardDrive,
  Search,
  Tag,
  Trash2,
  X,
} from "lucide-react";
import { motion, useReducedMotion } from "motion/react";
import { type ReactNode, useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { siGithub, siHuggingface, siKaggle, siModelscope } from "simple-icons";
import { Button } from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import {
  CenterMorphModal,
  CenterMorphModalContent,
} from "@/components/motion/center-morph-modal";
import { Input } from "@/components/motion/input";
import { AnimatedBadge } from "@/components/motion/animated-badge";
import {
  CopyButton,
  EmptyState,
  ErrorBox,
  Eyebrow,
  Inset,
  Loading,
  MetaTile,
  PageHeader,
  PageShell,
  Panel,
  SectionTitle,
  SelectField,
  Stat,
  useToast,
} from "@/components/ui";
import { cn } from "@/lib/utils";
import { api, formatBytes } from "../api.ts";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import { FileTree } from "../components/FileTree.tsx";
import { selectionSummary } from "../selection.ts";
import { sourceModelUrl } from "../source.ts";
import type {
  ArtifactDetail,
  ArtifactStorageStats,
  ArtifactSummary,
  Page,
  Provider,
} from "../types.ts";

const pageSize = 48;

type SortOption =
  | "created:desc"
  | "created:asc"
  | "name:asc"
  | "name:desc"
  | "size:desc"
  | "size:asc";

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

const providerLogos: Partial<Record<Provider, { hex: string; path: string }>> =
  {
    huggingface: siHuggingface,
    "modelscope-cn": siModelscope,
    "modelscope-ai": siModelscope,
    "github-release": siGithub,
    kaggle: siKaggle,
  };

const logoFrame =
  "grid size-7 shrink-0 place-items-center rounded-lg border border-border bg-background p-[5px] [&_svg]:size-full";

function SourceLogo({ provider }: { provider: Provider }) {
  const logo = providerLogos[provider];
  if (logo) {
    return (
      <span
        className={logoFrame}
        style={{ color: `#${logo.hex}` }}
        aria-hidden="true"
      >
        <svg viewBox="0 0 24 24">
          <path fill="currentColor" d={logo.path} />
        </svg>
      </span>
    );
  }
  if (provider === "http") {
    return (
      <span className={cn(logoFrame, "text-primary")} aria-hidden="true">
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
        >
          <circle cx="12" cy="12" r="8" />
          <path d="M4 12h16M12 4c2.2 2.3 3.3 5 3.3 8S14.2 17.7 12 20c-2.2-2.3-3.3-5-3.3-8S9.8 6.3 12 4Z" />
        </svg>
      </span>
    );
  }
  return (
    <span className={cn(logoFrame, "text-primary")} aria-hidden="true">
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
      >
        <path d="M4 7.5h6l1.8 2H20v8.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V7.5Z" />
        <path d="M4 10h16" />
      </svg>
    </span>
  );
}

const sourceOptions: Array<{ value: Provider | ""; label: string }> = [
  { value: "", label: "All sources" },
  ...providers,
];

const sortOptions: Array<{ value: SortOption; label: string }> = [
  { value: "created:desc", label: "Newest first" },
  { value: "created:asc", label: "Oldest first" },
  { value: "name:asc", label: "Name · A–Z" },
  { value: "name:desc", label: "Name · Z–A" },
  { value: "size:desc", label: "Size · largest first" },
  { value: "size:asc", label: "Size · smallest first" },
];

function ImmutableBadge() {
  return (
    <AnimatedBadge status="success" icon={<Gem />}>
      Immutable
    </AnimatedBadge>
  );
}

function artifactsPath(
  query: string,
  provider: Provider | "",
  sort: SortOption,
  offset: number,
) {
  const [sortBy, sortOrder] = sort.split(":") as [
    "created" | "name" | "size",
    "asc" | "desc",
  ];
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
  return (
    item.alias ??
    (item.name
      .normalize("NFKD")
      .replace(/[^a-zA-Z0-9._-]+/g, "-")
      .replace(/^-+|-+$/g, "") ||
      "model")
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
  command.push("--alias", shellArgument(artifactAlias(item)));
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
  const { notify } = useToast();
  const reduce = useReducedMotion();
  const [shownArtifactId, setShownArtifactId] = useState(artifactId);
  const [deletingArtifactId, setDeletingArtifactId] = useState<string>();
  const [artifactDetail, setArtifactDetail] = useState<ArtifactDetail>();
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [aliasInput, setAliasInput] = useState("");
  const [savingAlias, setSavingAlias] = useState(false);
  const [storageStats, setStorageStats] = useState<ArtifactStorageStats>();
  const [storageStatsLoading, setStorageStatsLoading] = useState(false);
  const [storageStatsError, setStorageStatsError] = useState("");
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
          if (active)
            setError(cause instanceof Error ? cause.message : String(cause));
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
    if (artifactId) setShownArtifactId(artifactId);
  }, [artifactId]);

  useEffect(() => {
    if (!artifactId) return;
    let active = true;
    setArtifactDetail(undefined);
    setDetailLoading(true);
    setDetailError("");
    setStorageStats(undefined);
    setStorageStatsError("");
    void api<ArtifactDetail>(`/artifacts/${encodeURIComponent(artifactId)}`)
      .then((result) => {
        if (!active) return;
        setArtifactDetail(result);
        setAliasInput(result.summary.alias ?? "");
      })
      .catch((cause) => {
        if (active)
          setDetailError(
            cause instanceof Error ? cause.message : String(cause),
          );
      })
      .finally(() => {
        if (active) setDetailLoading(false);
      });
    return () => {
      active = false;
    };
  }, [artifactId]);

  async function loadMore() {
    setLoading(true);
    setError("");
    try {
      const result = await api<Page<ArtifactSummary>>(
        artifactsPath(query, provider, sort, items.length),
      );
      setItems((current) => [...current, ...result.items]);
      setHasMore(result.hasMore);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }

  function copyFailed(message: string) {
    notify({
      status: "error",
      title: "Could not copy to clipboard",
      description: message,
    });
  }

  async function deleteArtifact(item: ArtifactSummary): Promise<boolean> {
    setDeletingArtifactId(item.artifactId);
    setError("");
    try {
      await api<void>(`/artifacts/${encodeURIComponent(item.artifactId)}`, {
        method: "DELETE",
      });
      setItems((current) =>
        current.filter((candidate) => candidate.artifactId !== item.artifactId),
      );
      if (artifactId === item.artifactId) navigate("/artifacts");
      notify({
        status: "success",
        title: "Artifact deleted",
        description: item.alias ?? item.name,
      });
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
      const summary = await api<ArtifactSummary>(
        `/artifacts/${encodeURIComponent(artifactDetail.summary.artifactId)}/alias`,
        {
          method: "PUT",
          body: JSON.stringify({ alias }),
        },
      );
      setArtifactDetail((current) =>
        current ? { ...current, summary } : current,
      );
      setItems((current) =>
        current.map((item) =>
          item.artifactId === summary.artifactId ? summary : item,
        ),
      );
      setAliasInput(summary.alias ?? "");
      notify({
        status: "success",
        title: alias ? "Alias saved" : "Alias removed",
        description: alias ?? summary.name,
      });
    } catch (cause) {
      setDetailError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSavingAlias(false);
    }
  }

  async function calculateStorageStats() {
    if (!artifactDetail) return;
    setStorageStatsLoading(true);
    setStorageStatsError("");
    try {
      const result = await api<ArtifactStorageStats>(
        `/artifacts/${encodeURIComponent(artifactDetail.summary.artifactId)}/storage-stats`,
        {
          method: "POST",
        },
      );
      setStorageStats(result);
    } catch (cause) {
      setStorageStatsError(
        cause instanceof Error ? cause.message : String(cause),
      );
    } finally {
      setStorageStatsLoading(false);
    }
  }

  const detail =
    artifactDetail?.summary.artifactId === shownArtifactId
      ? artifactDetail
      : undefined;
  const closeDetail = useCallback(() => navigate("/artifacts"), [navigate]);
  const changeDetailOpen = useCallback(
    (open: boolean) => {
      if (!open) closeDetail();
    },
    [closeDetail],
  );
  return (
    <PageShell>
      <PageHeader
        eyebrow="Immutable storage"
        title="Artifacts"
        description="Only fully verified artifacts atomically added to this shelf appear here."
      />
      <div className="relative z-20 mb-6 grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(260px,1fr)_220px_220px]">
        <Input
          className="sm:col-span-2 lg:col-span-1"
          aria-label="Search artifacts"
          placeholder="Search name, model ID or revision…"
          leftIcon={<Search />}
          value={query}
          onChange={setQuery}
        />
        <SelectField
          label="Source"
          value={provider}
          options={sourceOptions}
          onChange={setProvider}
        />
        <SelectField
          label="Sort"
          value={sort}
          options={sortOptions}
          onChange={setSort}
        />
      </div>
      {error && <ErrorBox className="mb-4">{error}</ErrorBox>}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {items.map((item, index) => {
          const sourceUrl = sourceModelUrl(
            item.provider,
            item.sourceId,
            item.resolvedRevision,
          );
          const command = addCommand(item);
          const title = item.alias ?? item.name;
          return (
            <motion.article
              key={item.artifactId}
              layout={reduce ? false : "position"}
              initial={reduce ? false : { opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{
                duration: 0.35,
                delay: Math.min(index, 8) * 0.03,
                ease: [0.16, 1, 0.3, 1],
              }}
              className="flex min-h-full min-w-0 flex-col rounded-[28px] border border-border bg-card p-5"
            >
              <div className="grid gap-3">
                <div className="grid min-w-0 gap-0.5">
                  <h2 className="min-w-0 text-lg font-semibold leading-tight tracking-tight [overflow-wrap:anywhere]">
                    {title}
                  </h2>
                  {item.alias && (
                    <span
                      className="truncate text-xs text-muted-foreground"
                      title={item.sourceId}
                    >
                      {item.name}
                    </span>
                  )}
                  {sourceUrl ? (
                    <a
                      className="group mt-1.5 inline-flex max-w-full items-center gap-1 self-start text-[13px] text-muted-foreground transition-colors hover:text-primary"
                      href={sourceUrl}
                      target="_blank"
                      rel="noreferrer"
                    >
                      <span className="truncate">{item.sourceId}</span>
                      <ExternalLink className="size-3 shrink-0" aria-hidden />
                    </a>
                  ) : (
                    <span className="mt-1.5 truncate text-[13px] text-muted-foreground">
                      {item.sourceId}
                    </span>
                  )}
                </div>
                <div className="mb-5 flex items-center justify-between gap-3">
                  <ImmutableBadge />
                  {canManage && (
                    <div className="flex shrink-0 items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`${item.alias ? "Change" : "Set"} alias for ${title}`}
                        title={item.alias ? "Change alias" : "Set alias"}
                        onClick={() =>
                          navigate(
                            `/artifacts/${encodeURIComponent(item.artifactId)}`,
                          )
                        }
                      >
                        <Tag className="size-4" aria-hidden />
                      </Button>
                      <DeleteConfirm
                        triggerLabel={`Delete ${title}`}
                        triggerIcon={<Trash2 className="size-4" aria-hidden />}
                        triggerSize="icon"
                        title={`Delete ${item.name}?`}
                        description={`Its manifest and all ${item.fileCount.toLocaleString()} files will be permanently removed. Logical size: ${formatBytes(item.totalSize)}. Because immutable files may be hardlinked across artifacts, the physical space released can be smaller.`}
                        confirmLabel="Delete artifact"
                        disabled={deletingArtifactId === item.artifactId}
                        onConfirm={() => deleteArtifact(item)}
                      />
                    </div>
                  )}
                </div>
              </div>
              <dl className="grid flex-1 content-start gap-3">
                <CardFact label="Source">
                  <span className="flex min-w-0 items-center gap-2 font-medium">
                    <SourceLogo provider={item.provider} />
                    <span className="truncate">
                      {providerLabel(item.provider)}
                    </span>
                  </span>
                </CardFact>
                <CardFact label="Resolved revision">
                  <span
                    className="block truncate font-mono text-[0.87em]"
                    title={item.resolvedRevision}
                  >
                    {item.resolvedRevision}
                  </span>
                </CardFact>
                <CardFact label="Content">
                  {item.fileCount.toLocaleString()} files ·{" "}
                  {formatBytes(item.totalSize)}
                </CardFact>
                <CardFact label="Selection">
                  <span
                    className="block truncate"
                    title={selectionSummary(item.selectedPaths)}
                  >
                    {selectionSummary(item.selectedPaths)}
                  </span>
                </CardFact>
                <CardFact label="Added to shelf">
                  {new Date(item.createdAt).toLocaleString()}
                </CardFact>
              </dl>
              <div className="mt-5 grid gap-2.5 border-t border-border pt-4.5">
                <Inset className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 rounded-full py-1 pl-3.5 pr-1">
                  <code
                    className="block min-w-0 truncate font-mono text-[11px] text-muted-foreground"
                    title={command}
                  >
                    {command}
                  </code>
                  <CopyButton
                    variant="secondary"
                    size="sm"
                    value={command}
                    aria-label={`Copy modelshelf add command for ${item.name}`}
                    onCopyError={copyFailed}
                  >
                    Copy
                  </CopyButton>
                </Inset>
                <div className="grid gap-2 sm:grid-cols-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      navigate(
                        `/artifacts/${encodeURIComponent(item.artifactId)}`,
                      )
                    }
                  >
                    <Eye className="size-3.5" aria-hidden />
                    View details
                  </Button>
                  <CopyButton
                    variant="outline"
                    size="sm"
                    value={modelConfig(item)}
                    title="Copy this model entry, pinned to the resolved revision"
                    onCopyError={copyFailed}
                    icon={<Copy className="size-3.5" aria-hidden />}
                  >
                    Model config
                  </CopyButton>
                </div>
              </div>
            </motion.article>
          );
        })}
      </div>
      {loading && items.length === 0 && (
        <Loading className="py-16">Loading artifacts…</Loading>
      )}
      {hasMore && (
        <div className="flex justify-center pt-6">
          <StatefulButton
            variant="outline"
            state={loading ? "loading" : "idle"}
            loadingText="Loading…"
            onClick={() => void loadMore()}
          >
            Load more
          </StatefulButton>
        </div>
      )}
      {!loading && items.length === 0 && (
        <EmptyState title="No matching artifacts">
          Completed downloads will appear here.
        </EmptyState>
      )}
      <CenterMorphModal
        open={artifactId !== undefined}
        onOpenChange={changeDetailOpen}
      >
        <CenterMorphModalContent
          ariaLabel="Artifact details"
          showCloseButton={false}
          className="max-w-[980px]"
        >
          <div className="flex max-h-[calc(100dvh-6rem)] flex-col">
            <header className="flex items-start justify-between gap-3 border-b border-border px-4.5 pb-4.5 pt-5 sm:gap-6 sm:px-6">
              <div className="min-w-0">
                <Eyebrow>Artifact details</Eyebrow>
                <h2 className="text-xl font-semibold leading-tight tracking-tight [overflow-wrap:anywhere] sm:text-2xl">
                  {detail?.summary.alias ?? detail?.summary.name ?? "Artifact"}
                </h2>
                {detail?.summary.alias && (
                  <p className="mt-1 text-[13px] text-muted-foreground">
                    {detail.summary.name}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-2.5">
                <span className="hidden sm:block">
                  <ImmutableBadge />
                </span>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label="Close artifact details"
                  onClick={closeDetail}
                >
                  <X className="size-4" aria-hidden />
                </Button>
              </div>
            </header>
            <div className="grid min-h-0 gap-4 overflow-y-auto px-4.5 pb-5 pt-4 sm:px-6 sm:pb-6 sm:pt-5.5">
              {detailLoading && (
                <Loading className="min-h-[180px]">
                  Loading artifact details…
                </Loading>
              )}
              {detailError && <ErrorBox>{detailError}</ErrorBox>}
              {detail && (
                <>
                  {canManage && (
                    <Panel className="grid items-end gap-4 rounded-3xl p-4 md:grid-cols-[minmax(0,1fr)_minmax(280px,0.9fr)]">
                      <SectionTitle
                        icon={<Tag />}
                        title="Alias"
                        description="A unique, mutable label for this immutable artifact. It does not change its identity or storage path."
                      />
                      <div className="grid min-w-0 items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                        <Input
                          aria-label="Artifact alias"
                          value={aliasInput}
                          maxLength={128}
                          placeholder="Optional artifact alias"
                          onChange={setAliasInput}
                        />
                        <StatefulButton
                          state={savingAlias ? "loading" : "idle"}
                          loadingText="Saving…"
                          disabled={
                            aliasInput.trim() === (detail.summary.alias ?? "")
                          }
                          onClick={() => void saveAlias()}
                        >
                          {detail.summary.alias && !aliasInput.trim()
                            ? "Remove alias"
                            : "Save alias"}
                        </StatefulButton>
                      </div>
                    </Panel>
                  )}
                  <div className="grid gap-2.5 sm:grid-cols-2">
                    <MetaTile label="Source">
                      {sourceModelUrl(
                        detail.summary.provider,
                        detail.summary.sourceId,
                        detail.summary.resolvedRevision,
                      ) ? (
                        <a
                          className="inline-flex items-center gap-1 transition-colors hover:text-primary"
                          href={sourceModelUrl(
                            detail.summary.provider,
                            detail.summary.sourceId,
                            detail.summary.resolvedRevision,
                          )!}
                          target="_blank"
                          rel="noreferrer"
                        >
                          {providerLabel(detail.summary.provider)} ·{" "}
                          {detail.summary.sourceId}
                          <ExternalLink
                            className="size-3 shrink-0"
                            aria-hidden
                          />
                        </a>
                      ) : (
                        `${providerLabel(detail.summary.provider)} · ${detail.summary.sourceId}`
                      )}
                    </MetaTile>
                    <MetaTile label="Selection">
                      {selectionSummary(detail.summary.selectedPaths)}
                    </MetaTile>
                    <MetaTile label="Resolved revision" mono>
                      {detail.summary.resolvedRevision}
                    </MetaTile>
                    <MetaTile label="Content">
                      {detail.summary.fileCount.toLocaleString()} files ·{" "}
                      {formatBytes(detail.summary.totalSize)}
                    </MetaTile>
                    <MetaTile label="Content SHA-256" mono>
                      {detail.manifest.contentSha256}
                    </MetaTile>
                    <MetaTile label="Added to shelf">
                      {new Date(detail.summary.createdAt).toLocaleString()}
                    </MetaTile>
                    {detail.summary.selectionDigest && (
                      <MetaTile label="Selection digest" mono wide>
                        {detail.summary.selectionDigest}
                      </MetaTile>
                    )}
                    <MetaTile label="Storage path" mono wide>
                      {detail.summary.relativePath}
                    </MetaTile>
                  </div>
                  {canManage && (
                    <Panel className="grid gap-3 rounded-3xl p-4">
                      <SectionTitle
                        icon={<HardDrive />}
                        title="Disk usage"
                        description="Calculated on demand from file allocation and hardlink metadata. This scan may wake sleeping disks."
                        action={
                          <StatefulButton
                            variant="outline"
                            size="sm"
                            state={storageStatsLoading ? "loading" : "idle"}
                            loadingText="Calculating…"
                            onClick={() => void calculateStorageStats()}
                          >
                            {storageStats
                              ? "Recalculate"
                              : "Calculate disk usage"}
                          </StatefulButton>
                        }
                      />
                      {storageStatsError && (
                        <ErrorBox>{storageStatsError}</ErrorBox>
                      )}
                      {storageStats && (
                        <>
                          <div className="grid gap-2.5 sm:grid-cols-2">
                            <Stat
                              label="Logical content"
                              value={formatBytes(storageStats.logicalSize)}
                              detail="Manifest file sizes"
                            />
                            <Stat
                              label="Shared via hardlinks"
                              value={formatBytes(
                                storageStats.sharedLogicalSize,
                              )}
                              detail={`${storageStats.sharedFileCount.toLocaleString()} files · ${formatBytes(storageStats.sharedAllocatedSize)} allocated`}
                            />
                            <Stat
                              label="Exclusive content"
                              value={formatBytes(
                                storageStats.exclusiveLogicalSize,
                              )}
                              detail={`${storageStats.exclusiveFileCount.toLocaleString()} files · ${formatBytes(storageStats.exclusiveAllocatedSize)} allocated`}
                            />
                            <Stat
                              label="Estimated reclaimable"
                              value={formatBytes(
                                storageStats.estimatedReclaimableSize,
                              )}
                              detail={`Includes ${formatBytes(storageStats.metadataAllocatedSize)} of manifest and directory metadata`}
                            />
                          </div>
                          <p className="text-xs leading-relaxed text-muted-foreground">
                            Allocated blocks referenced by this artifact:{" "}
                            {formatBytes(storageStats.allocatedSize)}.
                            Reclaimable space is an estimate; ZFS compression,
                            RAID-Z allocation and snapshots can make the
                            dataset-level change differ. Scanned{" "}
                            {new Date(storageStats.scannedAt).toLocaleString()}.
                          </p>
                        </>
                      )}
                    </Panel>
                  )}
                  <FileTree files={detail.manifest.files} />
                </>
              )}
            </div>
          </div>
        </CenterMorphModalContent>
      </CenterMorphModal>
    </PageShell>
  );
}

function CardFact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-[13px]">{children}</dd>
    </div>
  );
}
