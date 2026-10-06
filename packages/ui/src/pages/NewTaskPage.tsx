import {
  ArrowLeft,
  CheckCircle2,
  ExternalLink,
  Search,
  SlidersHorizontal,
} from "lucide-react";
import { type FormEvent, useEffect, useId, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { BouncyAccordion } from "@/components/motion/bouncy-accordion";
import { Button } from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import { Input } from "@/components/motion/input";
import { RadioGroup, RadioGroupItem } from "@/components/motion/radio";
import {
  CheckRow,
  ErrorBox,
  FieldHelp,
  Inset,
  Loading,
  Notice,
  PageHeader,
  PageShell,
  Panel,
  SectionTitle,
  SelectField,
  Stat,
  SwitchRow,
} from "@/components/ui";
import { localDateTimeValue } from "@/datetime";
import { api, formatBytes } from "../api.ts";
import { SearchField } from "../components/SearchField.tsx";
import { FileListFrame, SelectableFileTree } from "../components/FileTree.tsx";
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
  {
    value: "github-release",
    label: "GitHub Releases",
    hint: "owner/repository",
  },
  {
    value: "kaggle",
    label: "Kaggle Models",
    hint: "owner/model/framework/variation",
  },
  { value: "http", label: "Generic HTTP URL", hint: "https://…/model.tar.gz" },
];

type LookupState = "idle" | "loading" | "ready" | "error";
const lookupTimeoutMs = 35_000;

function defaultRevision(provider: Provider): string {
  if (provider === "modelscope-cn" || provider === "modelscope-ai")
    return "master";
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
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      parsed.hostname.length > 0 &&
      parsed.username.length === 0 &&
      parsed.password.length === 0
    );
  } catch {
    return false;
  }
}

function lookup<T>(path: string, controller: AbortController): Promise<T> {
  const timeout = window.setTimeout(() => {
    controller.abort(
      new DOMException(
        `Provider lookup timed out after ${lookupTimeoutMs / 1_000} seconds.`,
        "TimeoutError",
      ),
    );
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
        setServerInfoError(
          cause instanceof Error ? cause.message : String(cause),
        );
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
      )
        .then((result) => {
          setModelOptions(result.models);
          setModelLookup("ready");
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setModelOptions([]);
          setModelLookup("error");
          setModelLookupError(
            cause instanceof Error ? cause.message : String(cause),
          );
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
    const ready =
      provider === "http"
        ? /^https?:\/\//i.test(sourceId) && requestedRevision.length > 0
        : hasCompleteModelId(provider, sourceId) &&
          requestedRevision.length > 0 &&
          (!useTemporaryMirror || isHttpUrl(temporaryMirror));
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
      )
        .then((result) => {
          setEstimate(result);
          setEstimateKey(key);
          setEstimateLookup("ready");
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setEstimate(null);
          setEstimateKey("");
          setEstimateLookup("error");
          setEstimateError(
            cause instanceof Error ? cause.message : String(cause),
          );
        });
    }, 500);

    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [
    disableMirror,
    disableProxy,
    id,
    mirrorUrl,
    provider,
    revision,
    useTemporaryMirror,
  ]);

  useEffect(() => {
    setSelectFiles(false);
    setVariantFilter("");
    setSelectedPaths([]);
    setExpandedPaths(new Set());
  }, [
    estimate?.provider,
    estimate?.sourceId,
    estimate?.requestedRevision,
    estimate?.resolvedRevision,
  ]);

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
      )
        .then((result) => {
          setRevisionOptions(result.revisions);
          setRevisionLookup("ready");
          if (!revisionEdited.current) setRevision(result.defaultRevision);
        })
        .catch((cause: unknown) => {
          if (isAbortError(cause)) return;
          setRevisionOptions([]);
          setRevisionLookup("error");
          setRevisionLookupError(
            cause instanceof Error ? cause.message : String(cause),
          );
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
      if (
        scheduledStart &&
        (Number.isNaN(scheduledStart.getTime()) || scheduledStart <= new Date())
      ) {
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
  const supportsMirror =
    provider === "huggingface" ||
    provider === "modelscope-cn" ||
    provider === "modelscope-ai";
  const proxyConfigured = serverInfo?.network?.proxyConfigured === true;
  const minimumScheduledAt = localDateTimeValue(new Date(Date.now() + 60_000));
  const estimateIsCurrent =
    estimateLookup === "ready" &&
    estimateKey === currentEstimateKey &&
    estimate?.downloadable === true;
  const availableVariants = estimate?.ggufVariants ?? [];
  const selectableFiles = estimate?.selectableFiles ?? [];
  const usesGgufVariants =
    estimate?.ggufVariantSelectionAvailable === true &&
    availableVariants.length > 0;
  const normalizedFilter = variantFilter.trim().toLocaleLowerCase();
  const visibleVariants = normalizedFilter
    ? availableVariants.filter((variant) =>
        variant.label.toLocaleLowerCase().includes(normalizedFilter),
      )
    : availableVariants;
  const selectedVariant = availableVariants.find(
    (variant) =>
      variant.paths.length === selectedPaths.length &&
      variant.paths.every((path, index) => path === selectedPaths[index]),
  );
  const selectedPathSet = new Set(selectedPaths);
  const selectedFiles = selectableFiles.filter((file) =>
    selectedPathSet.has(file.path),
  );
  const selectedFilesSize = selectedFiles.every(
    (file) => file.size !== undefined,
  )
    ? selectedFiles.reduce((total, file) => total + (file.size ?? 0), 0)
    : undefined;
  const validSelection = usesGgufVariants
    ? selectedVariant !== undefined
    : selectedPaths.length > 0 && selectedFiles.length === selectedPaths.length;
  const displaySize = selectFiles
    ? usesGgufVariants
      ? selectedVariant?.totalSize
      : selectedFilesSize
    : estimate?.totalSize;
  const displayFileCount = selectFiles
    ? usesGgufVariants
      ? selectedVariant?.fileCount
      : selectedFiles.length
    : estimate?.fileCount;
  const reusablePathSet = new Set(estimate?.reusablePaths ?? []);
  const displayReusedSize = selectFiles
    ? selectedFiles
        .filter((file) => reusablePathSet.has(file.path))
        .reduce((total, file) => total + (file.size ?? 0), 0)
    : estimate?.reusedSize;
  const displayTransferSize =
    displaySize !== undefined && displayReusedSize !== undefined
      ? Math.max(0, displaySize - displayReusedSize)
      : estimate?.transferSize;
  const availableStorage = estimate?.availableStorageBytes;
  const storageSufficient =
    displayTransferSize === undefined || availableStorage === undefined
      ? estimate?.storageSufficient
      : displayTransferSize <= availableStorage;
  const duplicate =
    estimateIsCurrent && !selectFiles ? estimate?.duplicate : undefined;
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
  const providerLabel = providers.find(
    (item) => item.value === provider,
  )?.label;
  const mirrorInvalid = mirrorUrl.length > 0 && !isHttpUrl(mirrorUrl);
  const advanced = (
    <div className="grid gap-5 pt-1 text-sm text-foreground">
      <div className="grid gap-3">
        <SwitchRow
          title="Start this download later"
          description="The immutable revision is locked now. ModelShelf will not poll the source while it waits."
          checked={delayDownload}
          onCheckedChange={(checked) => {
            setDelayDownload(checked);
            if (checked && !scheduledAt)
              setScheduledAt(
                localDateTimeValue(new Date(Date.now() + 60 * 60_000)),
              );
          }}
        />
        {delayDownload && (
          <div className="grid gap-1.5">
            <Input
              label="Start time"
              type="datetime-local"
              value={scheduledAt}
              min={minimumScheduledAt}
              required
              onChange={setScheduledAt}
            />
            <FieldHelp>
              Uses your browser's local timezone. If the server is offline, the
              task starts after it comes back.
            </FieldHelp>
          </div>
        )}
      </div>
      {(supportsMirror || proxyConfigured) && (
        <Inset className="grid gap-4 p-4">
          <SectionTitle
            title="Network routing for this task"
            description="Preflight and the actual download will use the same choices below."
          />
          {supportsMirror && (
            <>
              <CheckRow
                checked={useTemporaryMirror}
                onCheckedChange={(checked) => {
                  setUseTemporaryMirror(checked);
                  if (checked) setDisableMirror(false);
                  invalidateEstimate();
                }}
                title="Use a temporary mirror for this task"
                description={
                  configuredMirror ? (
                    <>
                      This overrides the server mirror:{" "}
                      <code className="font-mono">{configuredMirror}</code>
                    </>
                  ) : (
                    "The address is stored with this task and does not change the server configuration."
                  )
                }
              />
              {useTemporaryMirror && (
                <div className="grid gap-1.5 pl-8">
                  <Input
                    label="Temporary mirror address"
                    type="url"
                    value={mirrorUrl}
                    placeholder="https://mirror.example.com"
                    autoComplete="url"
                    required
                    error={
                      mirrorInvalid
                        ? "Enter a valid HTTP(S) URL without embedded credentials."
                        : undefined
                    }
                    onChange={(value) => {
                      setMirrorUrl(value);
                      invalidateEstimate();
                    }}
                  />
                  {!mirrorInvalid && (
                    <FieldHelp>
                      HTTP(S) only. Do not include credentials in the URL.
                    </FieldHelp>
                  )}
                </div>
              )}
            </>
          )}
          {configuredMirror && !useTemporaryMirror && (
            <CheckRow
              checked={disableMirror}
              onCheckedChange={(checked) => {
                setDisableMirror(checked);
                invalidateEstimate();
              }}
              title="Bypass mirror for this task"
              description={
                <>
                  Mirror address for {providerLabel}:{" "}
                  <code className="font-mono" title={configuredMirror}>
                    {configuredMirror}
                  </code>
                </>
              }
            />
          )}
          {proxyConfigured && (
            <CheckRow
              checked={disableProxy}
              onCheckedChange={(checked) => {
                setDisableProxy(checked);
                invalidateEstimate();
              }}
              title="Bypass HTTP proxy for this task"
              description={
                <>
                  Server proxy:{" "}
                  <code className="font-mono">
                    {serverInfo?.network?.proxyDisplay ?? "configured"}
                  </code>
                </>
              }
            />
          )}
        </Inset>
      )}
    </div>
  );

  return (
    <PageShell narrow>
      <Link
        className="mb-6 inline-flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
        to="/tasks"
      >
        <ArrowLeft className="size-4" aria-hidden />
        Downloads
      </Link>
      <PageHeader eyebrow="New ingestion" title="Download a model" />
      {serverInfoError && (
        <ErrorBox className="mb-4">
          Could not load server network settings: {serverInfoError}
        </ErrorBox>
      )}
      <form
        className="grid min-w-0 grid-cols-1 gap-5"
        onSubmit={(event) => void submit(event)}
      >
        <Panel className="relative z-10 grid gap-5 p-5 sm:p-7">
          <SelectField
            label="Source"
            value={provider}
            options={providers}
            onChange={(nextProvider) => {
              setProvider(nextProvider);
              setId("");
              setRevision(defaultRevision(nextProvider));
              revisionEdited.current = false;
              setDisableMirror(false);
              setUseTemporaryMirror(false);
              setMirrorUrl("");
              invalidateEstimate();
            }}
          />
          <div className="grid gap-1.5">
            {provider === "http" ? (
              <Input
                label="URL"
                type="url"
                value={id}
                placeholder={selected.hint}
                onChange={(value) => {
                  setId(value);
                  invalidateEstimate();
                }}
              />
            ) : (
              <SearchField
                key={provider}
                label="Model ID"
                value={id}
                placeholder={selected.hint}
                remote
                options={modelOptions.map((model) => ({
                  value: model.id,
                  description: model.detail ?? model.name,
                }))}
                onChange={(value) => {
                  if (value === id) return;
                  setId(value);
                  setRevision(defaultRevision(provider));
                  revisionEdited.current = false;
                  invalidateEstimate();
                }}
              />
            )}
            {provider !== "http" && (
              <FieldHelp
                tone={modelLookup === "error" ? "error" : "muted"}
                className="min-h-5"
              >
                {modelLookup === "loading" && "Searching this hub…"}
                {modelLookup === "ready" &&
                  (modelOptions.length > 0
                    ? `${modelOptions.length} matching models — select one or keep typing any ID.`
                    : "No matching models found. You can still enter an exact ID.")}
                {modelLookup === "error" &&
                  `Search unavailable: ${modelLookupError}. You can still enter an exact ID.`}
                {modelLookup === "idle" &&
                  "Type at least 2 characters to search this hub, or enter an exact model ID."}
              </FieldHelp>
            )}
          </div>
          <div className="grid gap-1.5">
            {provider === "http" ? (
              <Input
                label="Requested revision"
                value={revision}
                onChange={(value) => {
                  setRevision(value);
                  invalidateEstimate();
                }}
              />
            ) : (
              <SearchField
                key={provider}
                label="Requested revision"
                value={revision}
                options={revisionOptions.map((option) => ({
                  value: option.name,
                  description: `${option.kind}${option.resolvedRevision ? ` · ${option.resolvedRevision.slice(0, 12)}` : ""}`,
                }))}
                onChange={(value) => {
                  if (value === revision) return;
                  setRevision(value);
                  revisionEdited.current = true;
                  invalidateEstimate();
                }}
              />
            )}
            <FieldHelp
              tone={revisionLookup === "error" ? "error" : "muted"}
              className="min-h-5"
            >
              {provider === "http" &&
                "The final immutable identity is computed from the downloaded content."}
              {provider !== "http" &&
                revisionLookup === "loading" &&
                "Loading branches, tags or versions from this hub…"}
              {provider !== "http" &&
                revisionLookup === "ready" &&
                (revisionOptions.length > 0
                  ? "Hub revisions loaded. The default was selected automatically; manual input is always allowed."
                  : "No named revisions were returned. Manual input is still allowed.")}
              {provider !== "http" &&
                revisionLookup === "error" &&
                `Revision lookup unavailable: ${revisionLookupError}. Manual input is still allowed.`}
              {provider !== "http" &&
                revisionLookup === "idle" &&
                "Enter a complete model ID to load hub revisions, or type a branch, tag, version or commit manually."}
            </FieldHelp>
          </div>
          <div className="grid gap-1.5">
            <Input
              label="Artifact alias (optional)"
              value={artifactAlias}
              maxLength={128}
              placeholder="For example: qwen-production"
              autoComplete="off"
              onChange={setArtifactAlias}
            />
            <FieldHelp>
              A unique label reserved when this task is created. It identifies
              the published artifact without changing its immutable identity or
              storage path.
            </FieldHelp>
          </div>
        </Panel>

        <BouncyAccordion
          collapsible
          classNames={{
            trigger: "gap-2 py-3 sm:gap-4",
            title: "whitespace-normal",
          }}
          items={[
            {
              id: "advanced",
              icon: <SlidersHorizontal className="size-4" />,
              title: (
                <>
                  Advanced{" "}
                  <span className="block text-xs font-normal text-muted-foreground sm:inline sm:text-sm">
                    {configuredMirror || proxyConfigured
                      ? "server routing is active"
                      : "routing and delayed start"}
                  </span>
                </>
              ),
              description: advanced,
            },
          ]}
        />

        <Panel className="grid gap-4 p-5 sm:p-7">
          <div aria-live="polite" className="grid gap-4">
            {estimateLookup === "idle" && (
              <SectionTitle
                title="Download preflight"
                description="Enter a complete model ID and requested revision to validate availability and estimate its size."
              />
            )}
            {estimateLookup === "loading" && (
              <div className="grid gap-2">
                <Loading className="justify-start">Checking download…</Loading>
                <FieldHelp className="px-0">
                  Validating access, revision and file metadata with the
                  selected provider.
                </FieldHelp>
              </div>
            )}
            {estimateLookup === "error" && (
              <ErrorBox title="Cannot validate this download">
                {estimateError}. Check the ID, revision and provider credentials
                before submitting.
              </ErrorBox>
            )}
            {estimateLookup === "ready" &&
              estimate &&
              estimateKey === currentEstimateKey && (
                <>
                  {duplicate && (
                    <Notice>
                      <strong className="font-semibold">
                        {duplicate.kind === "artifact"
                          ? "Already on this shelf"
                          : "Download task already exists"}
                      </strong>
                      <span className="text-muted-foreground">
                        {duplicate.kind === "artifact"
                          ? "This exact immutable revision is already stored. Starting another download would not create a second artifact."
                          : `This exact immutable revision already has a ${duplicate.taskStatus?.replaceAll("_", " ") ?? "running"} task. A duplicate submission would reuse it.`}
                      </span>
                    </Notice>
                  )}
                  <div className="flex items-center gap-2 text-sm font-medium text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 className="size-4 shrink-0" aria-hidden />
                    Revision exists and is accessible with the configured
                    credentials.
                  </div>
                  <div className="grid gap-3 sm:grid-cols-2">
                    <Stat
                      label={
                        displayReusedSize
                          ? "Artifact content"
                          : "Estimated download"
                      }
                      value={
                        displaySize === undefined
                          ? "Size unavailable"
                          : formatBytes(displaySize)
                      }
                    />
                    <Stat
                      label="Files"
                      value={
                        displayFileCount === undefined
                          ? "Unknown"
                          : displayFileCount.toLocaleString()
                      }
                    />
                    {!!displayReusedSize && (
                      <Stat
                        label="Network download"
                        value={
                          displayTransferSize === undefined
                            ? "Size unavailable"
                            : formatBytes(displayTransferSize)
                        }
                      />
                    )}
                    {!!displayReusedSize && (
                      <Stat
                        label="Reused from shelf"
                        value={formatBytes(displayReusedSize)}
                      />
                    )}
                    {availableStorage !== undefined && (
                      <Stat
                        label="Storage available"
                        value={formatBytes(availableStorage)}
                      />
                    )}
                  </div>
                  {storageSufficient === false && (
                    <ErrorBox>
                      Insufficient storage for the estimated{" "}
                      {displayTransferSize === undefined
                        ? "download"
                        : formatBytes(displayTransferSize)}{" "}
                      of new data.
                    </ErrorBox>
                  )}
                  {estimate.fileSelectionAvailable &&
                    (usesGgufVariants || selectableFiles.length > 0) && (
                      <Inset className="grid gap-3 p-4">
                        <SwitchRow
                          title={
                            usesGgufVariants
                              ? "Download a specific GGUF variant"
                              : "Select files to download"
                          }
                          description={
                            usesGgufVariants
                              ? "Only complete, unambiguous variants are offered. All shards in the selected variant are downloaded together."
                              : "Advanced option for repositories that contain several independent formats or variants."
                          }
                          checked={selectFiles}
                          onCheckedChange={(checked) => {
                            setSelectFiles(checked);
                            if (!checked) setSelectedPaths([]);
                          }}
                        />
                        {selectFiles && (
                          <div className="grid gap-3 border-t border-border pt-3">
                            {!usesGgufVariants && (
                              <Notice>
                                <strong className="font-semibold">
                                  Manual selection can produce an unusable
                                  artifact.
                                </strong>
                                <span className="text-muted-foreground">
                                  ModelShelf cannot determine this repository's
                                  runtime dependencies. Include every required
                                  weight shard, index, config, tokenizer and
                                  custom code file.
                                </span>
                              </Notice>
                            )}
                            <div className="grid items-center gap-2 sm:grid-cols-[minmax(0,1fr)_auto]">
                              <Input
                                type="search"
                                value={variantFilter}
                                placeholder={
                                  usesGgufVariants
                                    ? "Filter GGUF variants"
                                    : "Filter files"
                                }
                                aria-label={
                                  usesGgufVariants
                                    ? "Filter GGUF variants"
                                    : "Filter files"
                                }
                                leftIcon={<Search />}
                                onChange={setVariantFilter}
                              />
                              {!usesGgufVariants && (
                                <div className="flex gap-1.5">
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() =>
                                      setSelectedPaths(
                                        selectableFiles.map(
                                          (file) => file.path,
                                        ),
                                      )
                                    }
                                  >
                                    All
                                  </Button>
                                  <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => setSelectedPaths([])}
                                  >
                                    None
                                  </Button>
                                </div>
                              )}
                            </div>
                            <FileListFrame>
                              {usesGgufVariants && (
                                <RadioGroup
                                  className="gap-0"
                                  value={selectedVariant?.label ?? ""}
                                  onValueChange={(label) => {
                                    const variant = availableVariants.find(
                                      (item) => item.label === label,
                                    );
                                    if (variant)
                                      setSelectedPaths(variant.paths);
                                  }}
                                >
                                  {visibleVariants.map((variant) => (
                                    <VariantOption
                                      key={variant.label}
                                      label={variant.label}
                                      paths={variant.paths}
                                      detail={`${variant.fileCount > 1 ? `${variant.fileCount} shards · ` : ""}${variant.totalSize === undefined ? "size unavailable" : formatBytes(variant.totalSize)}`}
                                    />
                                  ))}
                                </RadioGroup>
                              )}
                              {!usesGgufVariants && (
                                <SelectableFileTree
                                  files={selectableFiles}
                                  query={variantFilter}
                                  selected={selectedPathSet}
                                  expanded={expandedPaths}
                                  onSelectionChange={toggleSelectedPaths}
                                  onExpand={toggleExpandedPath}
                                />
                              )}
                              {usesGgufVariants &&
                                visibleVariants.length === 0 && (
                                  <p className="px-4 py-5 text-center text-sm text-muted-foreground">
                                    No variants match this filter.
                                  </p>
                                )}
                            </FileListFrame>
                            <div
                              className={
                                validSelection
                                  ? "grid gap-0.5 text-sm"
                                  : "text-sm text-destructive"
                              }
                            >
                              {validSelection ? (
                                <>
                                  <strong className="font-semibold">
                                    {selectionSummary(selectedPaths)}
                                  </strong>
                                  <span className="text-xs text-muted-foreground">
                                    {displayFileCount?.toLocaleString()}{" "}
                                    {displayFileCount === 1 ? "file" : "files"}{" "}
                                    selected
                                    {displaySize === undefined
                                      ? ""
                                      : ` · ${formatBytes(displaySize)}`}
                                  </span>
                                </>
                              ) : usesGgufVariants ? (
                                "Select one GGUF variant."
                              ) : (
                                "Select at least one file."
                              )}
                            </div>
                            {usesGgufVariants &&
                              estimate.ggufAuxiliaryFiles &&
                              estimate.ggufAuxiliaryFiles.length > 0 && (
                                <Notice>
                                  Auxiliary GGUF files such as projectors are
                                  not included. Download the full repository if
                                  your runtime needs them.
                                </Notice>
                              )}
                          </div>
                        )}
                      </Inset>
                    )}
                  {estimate.resolvedRevision && (
                    <div className="grid gap-1">
                      <span className="text-xs text-muted-foreground">
                        Resolved immutable revision
                      </span>
                      <code className="break-all font-mono text-xs">
                        {estimate.resolvedRevision}
                      </code>
                    </div>
                  )}
                  {estimate.metadata.length > 0 && (
                    <dl className="grid gap-x-5 gap-y-3 border-t border-border pt-4 sm:grid-cols-2">
                      {estimate.metadata.map((item) => (
                        <div
                          className="grid min-w-0 gap-0.5"
                          key={`${item.label}-${item.value}`}
                        >
                          <dt className="text-xs text-muted-foreground">
                            {item.label}
                          </dt>
                          <dd className="text-sm [overflow-wrap:anywhere]">
                            {item.value}
                          </dd>
                        </div>
                      ))}
                    </dl>
                  )}
                  {estimate.hubUrl && (
                    <a
                      className="inline-flex w-max items-center gap-1 text-sm font-medium text-primary hover:underline hover:underline-offset-4"
                      href={estimate.hubUrl}
                      target="_blank"
                      rel="noreferrer"
                    >
                      Open model page
                      <ExternalLink className="size-3.5" aria-hidden />
                    </a>
                  )}
                  {displaySize === undefined && (
                    <FieldHelp className="px-0">
                      The provider confirmed that the revision is downloadable
                      but did not expose reliable size metadata.
                    </FieldHelp>
                  )}
                </>
              )}
          </div>
        </Panel>
        {provider === "http" && (
          <Notice>
            <strong className="font-semibold">Two-stage download</strong>
            <span className="text-muted-foreground">
              The URL will only be downloaded into staging. After it finishes,
              you must review inferred metadata and explicitly choose whether to
              extract it before anything is published.
            </span>
          </Notice>
        )}
        {error && <ErrorBox>{error}</ErrorBox>}
        <div className="flex flex-wrap justify-end gap-2.5">
          <Button variant="outline" onClick={() => navigate("/tasks")}>
            Cancel
          </Button>
          {duplicate?.taskId ? (
            <Button
              variant="secondary"
              onClick={() => navigate(`/tasks/${duplicate.taskId}`)}
            >
              Open existing task
            </Button>
          ) : duplicate?.kind === "artifact" ? (
            <Button variant="secondary" onClick={() => navigate("/artifacts")}>
              View artifact
            </Button>
          ) : (
            <StatefulButton
              type="submit"
              state={busy || estimateLookup === "loading" ? "loading" : "idle"}
              loadingText={busy ? "Submitting…" : "Validating…"}
              disabled={
                !estimateIsCurrent ||
                storageSufficient === false ||
                (selectFiles && !validSelection)
              }
            >
              {delayDownload ? "Schedule download" : "Start download"}
            </StatefulButton>
          )}
        </div>
      </form>
    </PageShell>
  );
}

function VariantOption({
  label,
  paths,
  detail,
}: {
  label: string;
  paths: string[];
  detail: string;
}) {
  const id = useId();
  return (
    <div className="flex items-center gap-3 border-t border-border px-3.5 py-2.5 transition-colors first:border-t-0 hover:bg-muted/70">
      <RadioGroupItem id={id} value={label} />
      <label htmlFor={id} className="grid min-w-0 cursor-pointer gap-0.5">
        <code className="truncate font-mono text-xs" title={paths.join("\n")}>
          {label}
        </code>
        <small className="text-[11px] text-muted-foreground">{detail}</small>
      </label>
    </div>
  );
}
