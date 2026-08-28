import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { api, formatDownloadSize } from "../api.ts";
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

export function NewTaskPage() {
  const [provider, setProvider] = useState<Provider>("huggingface");
  const [id, setId] = useState("");
  const [revision, setRevision] = useState("main");
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null);
  const [disableMirror, setDisableMirror] = useState(false);
  const [disableProxy, setDisableProxy] = useState(false);
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
      void api<ModelSearch>(
        `/providers/${provider}/models?q=${encodeURIComponent(query)}`,
        { signal: controller.signal },
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
    const ready = provider === "http"
      ? /^https?:\/\//i.test(sourceId) && requestedRevision.length > 0
      : hasCompleteModelId(provider, sourceId) && requestedRevision.length > 0;
    if (!ready) {
      setEstimate(null);
      setEstimateKey("");
      setEstimateLookup("idle");
      setEstimateError("");
      return;
    }

    const key = `${provider}\u0000${sourceId}\u0000${requestedRevision}\u0000${disableMirror}\u0000${disableProxy}`;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setEstimateLookup("loading");
      setEstimateError("");
      void api<DownloadEstimate>(
        `/providers/${provider}/estimate?id=${encodeURIComponent(sourceId)}&revision=${encodeURIComponent(requestedRevision)}&disableMirror=${disableMirror}&disableProxy=${disableProxy}`,
        { signal: controller.signal },
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
  }, [disableMirror, disableProxy, id, provider, revision]);

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
      void api<RevisionDiscovery>(
        `/providers/${provider}/revisions?id=${encodeURIComponent(sourceId)}`,
        { signal: controller.signal },
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
      const task = await api<DownloadTask>("/tasks", {
        method: "POST",
        body: JSON.stringify({
          provider,
          id: id.trim(),
          revision: revision.trim(),
          disableMirror,
          disableProxy,
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
  const currentEstimateKey = `${provider}\u0000${id.trim()}\u0000${revision.trim()}\u0000${disableMirror}\u0000${disableProxy}`;
  const configuredMirror = serverInfo?.network?.mirrors[provider];
  const proxyConfigured = serverInfo?.network?.proxyConfigured === true;
  const estimateIsCurrent = estimateLookup === "ready"
    && estimateKey === currentEstimateKey
    && estimate?.downloadable === true;
  const duplicate = estimateIsCurrent ? estimate?.duplicate : undefined;
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
      {(configuredMirror || proxyConfigured) && <aside className="network-policy">
        <div><strong>Server network routing is active</strong><span>Preflight and the actual download will use the same choices below.</span></div>
        {configuredMirror && <label className="route-option">
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
            <div><span>Estimated download</span><strong>{estimate.totalSize === undefined ? "Size unavailable" : formatDownloadSize(estimate.totalSize)}</strong></div>
            <div><span>Files</span><strong>{estimate.fileCount === undefined ? "Unknown" : estimate.fileCount.toLocaleString()}</strong></div>
          </div>
          <div className="estimate-valid">✓ Revision exists and is accessible with the configured credentials.</div>
          {estimate.hubUrl && <a className="estimate-hub-link" href={estimate.hubUrl} target="_blank" rel="noreferrer">Open model page ↗</a>}
          {estimate.resolvedRevision && <div className="estimate-revision"><span>Resolved immutable revision</span><code>{estimate.resolvedRevision}</code></div>}
          {estimate.metadata.length > 0 && <dl className="estimate-metadata">
            {estimate.metadata.map((item) => <div key={`${item.label}-${item.value}`}><dt>{item.label}</dt><dd>{item.value}</dd></div>)}
          </dl>}
          {estimate.totalSize === undefined && <span className="field-help">The provider confirmed that the revision is downloadable but did not expose reliable size metadata.</span>}
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
            : <button disabled={busy || !estimateIsCurrent}>{busy ? "Submitting…" : estimateLookup === "loading" ? "Validating…" : "Start download"}</button>}
      </div>
    </form>
  </div>;
}
