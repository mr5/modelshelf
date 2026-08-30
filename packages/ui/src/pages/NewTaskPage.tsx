import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, formatBytes } from "../api.ts";
import { SelectableFileTree } from "../components/FileTree.tsx";
import { selectionSummary } from "../selection.ts";
import type {
  DownloadEstimate,
  DownloadTask,
  ModelOption,
  ModelSearch,
  Provider,
  RevisionDiscovery,
  RevisionOption,
  ServerInfo,
} from "../types.ts";

const providers: { value: Provider; label: string; hint: string }[] = [
  { value: "huggingface", label: "Hugging Face Hub", hint: "owner/model" },
  { value: "modelscope-cn", label: "ModelScope CN", hint: "owner/model" },
  { value: "modelscope-ai", label: "ModelScope AI", hint: "owner/model" },
  { value: "github-release", label: "GitHub Releases", hint: "owner/repository" },
  { value: "kaggle", label: "Kaggle Models", hint: "owner/model/framework/variation" },
  { value: "http", label: "Generic HTTP URL", hint: "https://…/model.tar.gz" },
];

type LookupState = "idle" | "loading" | "ready" | "error";
const lookupTimeoutMs = 35_000;

function defaultRevision(provider: Provider): string {
  if (provider === "modelscope-cn" || provider === "modelscope-ai") return "master";
  if (provider === "github-release" || provider === "kaggle") return "latest";
  if (provider === "http") return "content";
  return "main";
}

function hasCompleteModelId(provider: Provider, sourceId: string): boolean {
  const parts = sourceId.split("/").filter(Boolean);
  if (provider === "kaggle") return parts.length === 4;
  if (provider === "http") return false;
  return parts.length === 2;
}

function isAbortError(cause: unknown): boolean {
  return cause instanceof DOMException && cause.name === "AbortError";
}

function isHttpUrl(value: string): boolean {
  try {
    const parsed = new URL(value);
    return (parsed.protocol === "http:" || parsed.protocol === "https:")
      && parsed.hostname.length > 0
      && parsed.username.length === 0
      && parsed.password.length === 0;
  } catch {
    return false;
  }
}

function localDateTimeValue(date: Date): string {
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function lookup<T>(path: string, controller: AbortController): Promise<T> {
  const timeout = window.setTimeout(() => {
    controller.abort(new DOMException(
      `Provider lookup timed out after ${lookupTimeoutMs / 1_000} seconds.`,
      "TimeoutError",
    ));
  }, lookupTimeoutMs);
  return api<T>(path, { signal: controller.signal }).finally(() => {
    window.clearTimeout(timeout);
  });
}

export function NewTaskPage() {
  const [provider, setProvider] = useState<Provider>("huggingface");
  const [id, setId] = useState("");
  const [revision, setRevision] = useState("main");
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null);
  const [disableMirror, setDisableMirror] = useState(false);
  const [useTemporaryMirror, setUseTemporaryMirror] = useState(false);
  const [mirrorUrl, setMirrorUrl] = useState("");
  const [disableProxy, setDisableProxy] = useState(false);
  const [delayDownload, setDelayDownload] = useState(false);
  const [scheduledAt, setScheduledAt] = useState("");
  const [selectFiles, setSelectFiles] = useState(false);
  const [selectedPaths, setSelectedPaths] = useState<string[]>([]);
  const [artifactAlias, setArtifactAlias] = useState("");
  const [variantFilter, setVariantFilter] = useState("");
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(new Set());
  const [modelOptions, setModelOptions] = useState<ModelOption[]>([]);
  const [revisionOptions, setRevisionOptions] = useState<RevisionOption[]>([]);
  const [modelLookup, setModelLookup] = useState<LookupState>("idle");
  const [revisionLookup, setRevisionLookup] = useState<LookupState>("idle");
  const [estimateLookup, setEstimateLookup] = useState<LookupState>("idle");
  const [estimate, setEstimate] = useState<DownloadEstimate | null>(null);
  const [estimateKey, setEstimateKey] = useState("");
  const [modelLookupError, setModelLookupError] = useState("");
  const [revisionLookupError, setRevisionLookupError] = useState("");
  const [estimateError, setEstimateError] = useState("");
  const [serverInfoError, setServerInfoError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const revisionEdited = useRef(false);
  const navigate = useNavigate();

  function invalidateEstimate() {
    setEstimate(null);
    setEstimateKey("");
    setEstimateLookup("idle");
    setEstimateError("");
  }

  useEffect(() => {
    void api<ServerInfo>("/info")
      .then((info) => {
        setServerInfo(info);
        setServerInfoError("");
      })
      .catch((cause) => {
        setServerInfo(null);
        setServerInfoError(cause instanceof Error ? cause.message : String(cause));
      });
  }, []);

  useEffect(() => {
    const query = id.trim();
    if (provider === "http" || query.length < 2) {
      setModelOptions([]);
      setModelLookup("idle");
      setModelLookupError("");
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setModelLookup("loading");
      setModelLookupError("");
      void lookup<ModelSearch>(
        `/providers/${provider}/models?q=${encodeURIComponent(query)}`,
        controller,
      ).then((result) => {
        setModelOptions(result.models);
        setModelLookup("ready");
      }).catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setModelOptions([]);
        setModelLookup("error");
        setModelLookupError(cause instanceof Error ? cause.message : String(cause));
      });
    }, 350);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [id, provider]);

  useEffect(() => {
    const sourceId = id.trim();
    const requestedRevision = revision.trim();
    const temporaryMirror = useTemporaryMirror ? mirrorUrl.trim() : "";
    const ready = provider === "http"
      ? /^https?:\/\//i.test(sourceId) && requestedRevision.length > 0
      : hasCompleteModelId(provider, sourceId)
        && requestedRevision.length > 0
        && (!useTemporaryMirror || isHttpUrl(temporaryMirror));
    if (!ready) {
      setEstimate(null);
      setEstimateKey("");
      setEstimateLookup("idle");
      setEstimateError("");
      return;
    }

    const key = `${provider}\u0000${sourceId}\u0000${requestedRevision}\u0000${disableMirror}\u0000${temporaryMirror}\u0000${disableProxy}`;
    const params = new URLSearchParams({
      id: sourceId,
      revision: requestedRevision,
      disableMirror: String(disableMirror),
      disableProxy: String(disableProxy),
    });
    if (temporaryMirror) params.set("mirrorUrl", temporaryMirror);
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setEstimateLookup("loading");
      setEstimateError("");
      void lookup<DownloadEstimate>(
        `/providers/${provider}/estimate?${params.toString()}`,
        controller,
      ).then((result) => {
        setEstimate(result);
        setEstimateKey(key);
        setEstimateLookup("ready");
      }).catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setEstimate(null);
        setEstimateKey("");
        setEstimateLookup("error");
        setEstimateError(cause instanceof Error ? cause.message : String(cause));
      });
    }, 500);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [disableMirror, disableProxy, id, mirrorUrl, provider, revision, useTemporaryMirror]);

  useEffect(() => {
    setSelectFiles(false);
    setVariantFilter("");
    setSelectedPaths([]);
    setExpandedPaths(new Set());
  }, [estimate?.provider, estimate?.sourceId, estimate?.requestedRevision, estimate?.resolvedRevision]);

  useEffect(() => {
    const sourceId = id.trim();
    if (!hasCompleteModelId(provider, sourceId)) {
      setRevisionOptions([]);
      setRevisionLookup("idle");
      setRevisionLookupError("");
      return;
    }

    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setRevisionLookup("loading");
      setRevisionLookupError("");
      void lookup<RevisionDiscovery>(
        `/providers/${provider}/revisions?id=${encodeURIComponent(sourceId)}`,
        controller,
      ).then((result) => {
        setRevisionOptions(result.revisions);
        setRevisionLookup("ready");
        if (!revisionEdited.current) setRevision(result.defaultRevision);
      }).catch((cause: unknown) => {
        if (isAbortError(cause)) return;
        setRevisionOptions([]);
        setRevisionLookup("error");
        setRevisionLookupError(cause instanceof Error ? cause.message : String(cause));
      });
    }, 450);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [id, provider]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const scheduledStart = delayDownload ? new Date(scheduledAt) : null;
      if (scheduledStart && (Number.isNaN(scheduledStart.getTime()) || scheduledStart <= new Date())) {
        throw new Error("Choose a scheduled start time in the future.");
      }
      const task = await api<DownloadTask>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          provider,
          id: id.trim(),
          revision: revision.trim(),
          disableMirror,
          mirrorUrl: useTemporaryMirror ? mirrorUrl.trim() : undefined,
          disableProxy,
          scheduledAt: scheduledStart?.toISOString(),
          selectedPaths: selectFiles ? selectedPaths : undefined,
          alias: artifactAlias.trim() || undefined,
        }),
      });
      navigate(`/tasks/${task.id}`);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  const selected = providers.find((item) => item.value === provider)!;
  const temporaryMirror = useTemporaryMirror ? mirrorUrl.trim() : "";
  const currentEstimateKey = `${provider}\u0000${id.trim()}\u0000${revision.trim()}\u0000${disableMirror}\u0000${temporaryMirror}\u0000${disableProxy}`;
  const configuredMirror = serverInfo?.network?.mirrors[provider];
  const supportsMirror = provider === "huggingface"
    || provider === "modelscope-cn"
    || provider === "modelscope-ai";
  const proxyConfigured = serverInfo?.network?.proxyConfigured === true;
  const minimumScheduledAt = localDateTimeValue(new Date(Date.now() + 60_000));
  const estimateIsCurrent = estimateLookup === "ready"
    && estimateKey === currentEstimateKey
    && estimate?.downloadable === true;
  const availableVariants = estimate?.ggufVariants ?? [];
  const selectableFiles = estimate?.selectableFiles ?? [];
  const usesGgufVariants = estimate?.ggufVariantSelectionAvailable === true
    && availableVariants.length > 0;
  const normalizedFilter = variantFilter.trim().toLocaleLowerCase();
  const visibleVariants = normalizedFilter
    ? availableVariants.filter((variant) => variant.label.toLocaleLowerCase().includes(normalizedFilter))
    : availableVariants;
  const selectedVariant = availableVariants.find((variant) =>
    variant.paths.length === selectedPaths.length
    && variant.paths.every((path, index) => path === selectedPaths[index])
  );
  const selectedPathSet = new Set(selectedPaths);
  const selectedFiles = selectableFiles.filter((file) => selectedPathSet.has(file.path));
  const selectedFilesSize = selectedFiles.every((file) => file.size !== undefined)
    ? selectedFiles.reduce((total, file) => total + (file.size ?? 0), 0)
    : undefined;
  const validSelection = usesGgufVariants
    ? selectedVariant !== undefined
    : selectedPaths.length > 0 && selectedFiles.length === selectedPaths.length;
  const displaySize = selectFiles
    ? usesGgufVariants ? selectedVariant?.totalSize : selectedFilesSize
    : estimate?.totalSize;
  const displayFileCount = selectFiles
    ? usesGgufVariants ? selectedVariant?.fileCount : selectedFiles.length
    : estimate?.fileCount;
  const reusablePathSet = new Set(estimate?.reusablePaths ?? []);
  const displayReusedSize = selectFiles
    ? selectedFiles
      .filter((file) => reusablePathSet.has(file.path))
      .reduce((total, file) => total + (file.size ?? 0), 0)
    : estimate?.reusedSize;
  const displayTransferSize = displaySize !== undefined && displayReusedSize !== undefined
    ? Math.max(0, displaySize - displayReusedSize)
    : estimate?.transferSize;
  const availableStorage = estimate?.availableStorageBytes;
  const storageSufficient = displayTransferSize === undefined || availableStorage === undefined
    ? estimate?.storageSufficient
    : displayTransferSize <= availableStorage;
  const duplicate = estimateIsCurrent && !selectFiles ? estimate?.duplicate : undefined;
  function toggleSelectedPaths(paths: string[], checked: boolean) {
    setSelectedPaths((current) => {
      const next = new Set(current);
      for (const path of paths) checked ? next.add(path) : next.delete(path);
      return [...next].sort((left, right) => left.localeCompare(right));
    });
  }
  function toggleExpandedPath(path: string) {
    setExpandedPaths((current) => {
      const next = new Set(current);
      next.has(path) ? next.delete(path) : next.add(path);
      return next;
    });
  }
  return <div className="page narrow">
    <Link className="back" to="/tasks">← Downloads</Link>
    <div className="page-head"><div><p className="eyebrow">New ingestion</p><h1>Download a model</h1></div></div>
    {serverInfoError && <div className="error-box">Could not load server network settings: {serverInfoError}</div>}
    <form className="panel form-panel" onSubmit={(event) => void submit(event)}>
      <label>Source
        <select value={provider} onChange={(event) => {
          const nextProvider = event.target.value as Provider;
          setProvider(nextProvider);
          setId("");
          setRevision(defaultRevision(nextProvider));
          revisionEdited.current = false;
          setDisableMirror(false);
          setUseTemporaryMirror(false);
          setMirrorUrl("");
          invalidateEstimate();
        }}>
          {providers.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select>
      </label>
      <label>{provider === "http" ? "URL" : "Model ID"}
        <input
          value={id}
          list={provider === "http" ? undefined : "model-id-options"}
          placeholder={selected.hint}
          autoComplete="off"
          onChange={(event) => {
            setId(event.target.value);
            setRevision(defaultRevision(provider));
            revisionEdited.current = false;
            invalidateEstimate();
          }}
        />
        {provider !== "http" && <datalist id="model-id-options">
          {modelOptions.map((model) => <option key={model.id} value={model.id}>{model.detail ?? model.name}</option>)}
        </datalist>}
        {provider !== "http" && <span className={`field-help lookup ${modelLookup === "error" ? "lookup-error" : ""}`}>
          {modelLookup === "loading" && "Searching this hub…"}
          {modelLookup === "ready" && (modelOptions.length > 0 ? `${modelOptions.length} matching models — select one or keep typing any ID.` : "No matching models found. You can still enter an exact ID.")}
          {modelLookup === "error" && `Search unavailable: ${modelLookupError}. You can still enter an exact ID.`}
          {modelLookup === "idle" && "Type at least 2 characters to search this hub, or enter an exact model ID."}
        </span>}
      </label>
      <label>Requested revision
        <input
          value={revision}
          list={provider === "http" ? undefined : "revision-options"}
          onChange={(event) => {
            setRevision(event.target.value);
            revisionEdited.current = true;
            invalidateEstimate();
          }}
        />
        {provider !== "http" && <datalist id="revision-options">
          {revisionOptions.map((option) => <option key={option.name} value={option.name}>{option.kind}{option.resolvedRevision ? ` · ${option.resolvedRevision.slice(0, 12)}` : ""}</option>)}
        </datalist>}
        <span className={`field-help lookup ${revisionLookup === "error" ? "lookup-error" : ""}`}>
          {provider === "http" && "The final immutable identity is computed from the downloaded content."}
          {provider !== "http" && revisionLookup === "loading" && "Loading branches, tags or versions from this hub…"}
          {provider !== "http" && revisionLookup === "ready" && (revisionOptions.length > 0 ? "Hub revisions loaded. The default was selected automatically; manual input is always allowed." : "No named revisions were returned. Manual input is still allowed.")}
          {provider !== "http" && revisionLookup === "error" && `Revision lookup unavailable: ${revisionLookupError}. Manual input is still allowed.`}
          {provider !== "http" && revisionLookup === "idle" && "Enter a complete model ID to load hub revisions, or type a branch, tag, version or commit manually."}
        </span>
      </label>
      <label><span className="field-title">Artifact alias <small>Optional</small></span>
        <input
          value={artifactAlias}
          maxLength={128}
          placeholder="For example: qwen-production"
          autoComplete="off"
          onChange={(event) => setArtifactAlias(event.target.value)}
        />
        <span className="field-help">A unique label reserved when this task is created. It identifies the published artifact without changing its immutable identity or storage path.</span>
      </label>
      <details className="advanced-options">
        <summary><span><strong>Advanced</strong><small>{configuredMirror || proxyConfigured ? "Server routing is active · routing and delayed start" : "Routing and delayed start"}</small></span></summary>
        <div className="advanced-options-body">
          <section className="schedule-policy">
            <label className="route-option no-border">
              <input type="checkbox" checked={delayDownload} onChange={(event) => {
                const checked = event.target.checked;
                setDelayDownload(checked);
                if (checked && !scheduledAt) {
                  setScheduledAt(localDateTimeValue(new Date(Date.now() + 60 * 60_000)));
                }
              }} />
              <span><strong>Start this download later</strong><small>The immutable revision is locked now. ModelShelf will not poll the source while it waits.</small></span>
            </label>
            {delayDownload && <label className="route-input">Start time
              <input type="datetime-local" value={scheduledAt} min={minimumScheduledAt} required onChange={(event) => setScheduledAt(event.target.value)} />
              <small>Uses your browser's local timezone. If the server is offline, the task starts after it comes back.</small>
            </label>}
          </section>
          {(supportsMirror || proxyConfigured) && <aside className="network-policy">
            <div><strong>Network routing for this task</strong><span>Preflight and the actual download will use the same choices below.</span></div>
            {supportsMirror && <>
              <label className="route-option">
                <input type="checkbox" checked={useTemporaryMirror} onChange={(event) => {
                  const checked = event.target.checked;
                  setUseTemporaryMirror(checked);
                  if (checked) setDisableMirror(false);
                  invalidateEstimate();
                }} />
                <span>
                  <strong>Use a temporary mirror for this task</strong>
                  <small>{configuredMirror
                    ? <>This overrides the server mirror: <code>{configuredMirror}</code></>
                    : "The address is stored with this task and does not change the server configuration."}</small>
                </span>
              </label>
              {useTemporaryMirror && <label className="route-input">Temporary mirror address
                <input
                  type="url"
                  value={mirrorUrl}
                  placeholder="https://mirror.example.com"
                  autoComplete="url"
                  required
                  aria-invalid={mirrorUrl.length > 0 && !isHttpUrl(mirrorUrl)}
                  onChange={(event) => {
                    setMirrorUrl(event.target.value);
                    invalidateEstimate();
                  }}
                />
                <small className={mirrorUrl.length > 0 && !isHttpUrl(mirrorUrl) ? "lookup-error" : ""}>
                  {mirrorUrl.length > 0 && !isHttpUrl(mirrorUrl)
                    ? "Enter a valid HTTP(S) URL without embedded credentials."
                    : "HTTP(S) only. Do not include credentials in the URL."}
                </small>
              </label>}
            </>}
            {configuredMirror && !useTemporaryMirror && <label className="route-option">
              <input type="checkbox" checked={disableMirror} onChange={(event) => {
                setDisableMirror(event.target.checked);
                invalidateEstimate();
              }} />
              <span>
                <strong>Bypass mirror for this task</strong>
                <small className="route-detail">
                  <span>Mirror address for {providers.find((item) => item.value === provider)?.label}</span>
                  <code title={configuredMirror}>{configuredMirror}</code>
                </small>
              </span>
            </label>}
            {proxyConfigured && <label className="route-option">
              <input type="checkbox" checked={disableProxy} onChange={(event) => {
                setDisableProxy(event.target.checked);
                invalidateEstimate();
              }} />
              <span><strong>Bypass HTTP proxy for this task</strong><small>Server proxy: <code>{serverInfo?.network?.proxyDisplay ?? "configured"}</code></small></span>
            </label>}
          </aside>}
        </div>
      </details>
      <section className={`estimate-card ${estimateLookup === "error" ? "estimate-error" : ""}`} aria-live="polite">
        {estimateLookup === "idle" && <div><strong>Download preflight</strong><span>Enter a complete model ID and requested revision to validate availability and estimate its size.</span></div>}
        {estimateLookup === "loading" && <div><strong>Checking download…</strong><span>Validating access, revision and file metadata with the selected provider.</span></div>}
        {estimateLookup === "error" && <div><strong>Cannot validate this download</strong><span>{estimateError}. Check the ID, revision and provider credentials before submitting.</span></div>}
        {estimateLookup === "ready" && estimate && estimateKey === currentEstimateKey && <>
          {duplicate && <div className="duplicate-notice" role="status">
            <strong>{duplicate.kind === "artifact" ? "Already on this shelf" : "Download task already exists"}</strong>
            <span>{duplicate.kind === "artifact"
              ? "This exact immutable revision is already stored. Starting another download would not create a second artifact."
              : `This exact immutable revision already has a ${duplicate.taskStatus?.replaceAll("_", " ") ?? "running"} task. A duplicate submission would reuse it.`}</span>
            {duplicate.taskId && <Link to={`/tasks/${duplicate.taskId}`}>Open existing task →</Link>}
          </div>}
          <div className="estimate-summary">
            <div><span>{displayReusedSize ? "Artifact content" : "Estimated download"}</span><strong>{displaySize === undefined ? "Size unavailable" : formatBytes(displaySize)}</strong></div>
            <div><span>Files</span><strong>{displayFileCount === undefined ? "Unknown" : displayFileCount.toLocaleString()}</strong></div>
            {!!displayReusedSize && <div><span>Network download</span><strong>{displayTransferSize === undefined ? "Size unavailable" : formatBytes(displayTransferSize)}</strong></div>}
            {!!displayReusedSize && <div><span>Reused from shelf</span><strong>{formatBytes(displayReusedSize)}</strong></div>}
            {availableStorage !== undefined && <div><span>Storage available</span><strong>{formatBytes(availableStorage)}</strong></div>}
          </div>
          {storageSufficient === false && <div className="error-box">Insufficient storage for the estimated {displayTransferSize === undefined ? "download" : formatBytes(displayTransferSize)} of new data.</div>}
          {estimate.fileSelectionAvailable && (usesGgufVariants || selectableFiles.length > 0) && <section className="file-selection">
            <label className="route-option no-border">
              <input type="checkbox" checked={selectFiles} onChange={(event) => {
                const checked = event.target.checked;
                setSelectFiles(checked);
                if (!checked) setSelectedPaths([]);
              }} />
              <span><strong>{usesGgufVariants ? "Download a specific GGUF variant" : "Select files to download"}</strong><small>{usesGgufVariants ? "Only complete, unambiguous variants are offered. All shards in the selected variant are downloaded together." : "Advanced option for repositories that contain several independent formats or variants."}</small></span>
            </label>
            {selectFiles && <div className="file-selection-body">
              {!usesGgufVariants && <div className="file-selection-note" role="alert"><strong>Manual selection can produce an unusable artifact.</strong> ModelShelf cannot determine this repository's runtime dependencies. Include every required weight shard, index, config, tokenizer and custom code file.</div>}
              <div className="file-selection-tools">
                <input type="search" value={variantFilter} placeholder={usesGgufVariants ? "Filter GGUF variants" : "Filter files"} aria-label={usesGgufVariants ? "Filter GGUF variants" : "Filter files"} onChange={(event) => setVariantFilter(event.target.value)} />
                {!usesGgufVariants && <div><button type="button" className="ghost" onClick={() => setSelectedPaths(selectableFiles.map((file) => file.path))}>All</button><button type="button" className="ghost" onClick={() => setSelectedPaths([])}>None</button></div>}
              </div>
              <div className="file-selection-list">
                {usesGgufVariants && visibleVariants.map((variant) => {
                  const checked = variant.paths.length === selectedPaths.length
                    && variant.paths.every((path, index) => path === selectedPaths[index]);
                  return <label className="file-selection-item" key={variant.label}>
                    <input type="radio" name="gguf-variant" checked={checked} onChange={() => setSelectedPaths(variant.paths)} />
                    <span><code title={variant.paths.join("\n")}>{variant.label}</code><small>{variant.fileCount > 1 ? `${variant.fileCount} shards · ` : ""}{variant.totalSize === undefined ? "size unavailable" : formatBytes(variant.totalSize)}</small></span>
                  </label>;
                })}
                {!usesGgufVariants && <SelectableFileTree files={selectableFiles} query={variantFilter} selected={selectedPathSet} expanded={expandedPaths} onSelectionChange={toggleSelectedPaths} onExpand={toggleExpandedPath} />}
                {usesGgufVariants && visibleVariants.length === 0 && <p className="muted">No variants match this filter.</p>}
              </div>
              <div className={`file-selection-status ${validSelection ? "" : "invalid"}`}>{validSelection ? <><strong>{selectionSummary(selectedPaths)}</strong><span>{displayFileCount?.toLocaleString()} {displayFileCount === 1 ? "file" : "files"} selected{displaySize === undefined ? "" : ` · ${formatBytes(displaySize)}`}</span></> : usesGgufVariants ? "Select one GGUF variant." : "Select at least one file."}</div>
              {usesGgufVariants && estimate.ggufAuxiliaryFiles && estimate.ggufAuxiliaryFiles.length > 0 && <div className="file-selection-note">
                Auxiliary GGUF files such as projectors are not included. Download the full repository if your runtime needs them.
              </div>}
            </div>}
          </section>}
          <div className="estimate-valid">✓ Revision exists and is accessible with the configured credentials.</div>
          {estimate.hubUrl && <a className="estimate-hub-link" href={estimate.hubUrl} target="_blank" rel="noreferrer">Open model page ↗</a>}
          {estimate.resolvedRevision && <div className="estimate-revision"><span>Resolved immutable revision</span><code>{estimate.resolvedRevision}</code></div>}
          {estimate.metadata.length > 0 && <dl className="estimate-metadata">
            {estimate.metadata.map((item) => <div key={`${item.label}-${item.value}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}
          </dl>}
          {displaySize === undefined && <span className="field-help">The provider confirmed that the revision is downloadable but did not expose reliable size metadata.</span>}
        </>}
      </section>
      {provider === "http" && <div className="warning"><strong>Two-stage download</strong><span>The URL will only be downloaded into staging. After it finishes, you must review inferred metadata and explicitly choose whether to extract it before anything is published.</span></div>}
      {error && <div className="error-box">{error}</div>}
      <div className="actions">
        <Link className="ghost button" to="/tasks">Cancel</Link>
        {duplicate?.taskId
          ? <Link className="button existing-action" to={`/tasks/${duplicate.taskId}`}>Open existing task</Link>
          : duplicate?.kind === "artifact"
            ? <Link className="button existing-action" to="/artifacts">View artifact</Link>
            : <button disabled={busy || !estimateIsCurrent || storageSufficient === false || (selectFiles && !validSelection)}>{busy ? "Submitting…" : estimateLookup === "loading" ? "Validating…" : delayDownload ? "Schedule download" : "Start download"}</button>}
      </div>
    </form>
  </div>;
}
