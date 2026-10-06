import { Check, ExternalLink, X } from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useState,
} from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/motion/button/base";
import { StatefulButton } from "@/components/motion/button/stateful";
import {
  CenterMorphModal,
  CenterMorphModalContent,
} from "@/components/motion/center-morph-modal";
import { Input } from "@/components/motion/input";
import {
  CheckRow,
  ErrorBox,
  Eyebrow,
  Inset,
  labelText,
  Loading,
  MetaTile,
  Notice,
  Panel,
  ProgressBar,
  StatusBadge,
} from "@/components/ui";
import { cn } from "@/lib/utils";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import { FileTree } from "../components/FileTree.tsx";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import { ResumeControl } from "../components/ResumeControl.tsx";
import { ScheduleControl } from "../components/ScheduleControl.tsx";
import { sourceModelUrl } from "../source.ts";
import {
  taskStepProgress,
  taskSteps,
  type TaskStepView,
} from "../taskProgress.ts";
import type { DownloadTask } from "../types.ts";

/** Route-driven task detail modal: open whenever `/tasks/:id` is the current URL. */
export function TaskPage({
  taskId,
  onDeleted,
}: {
  taskId?: string;
  onDeleted?: (taskId: string) => void;
}) {
  const navigate = useNavigate();
  const [shownTaskId, setShownTaskId] = useState(taskId);
  useEffect(() => {
    if (taskId) setShownTaskId(taskId);
  }, [taskId]);
  const changeOpen = useCallback(
    (open: boolean) => {
      if (!open) navigate("/tasks", { replace: true });
    },
    [navigate],
  );
  return (
    <CenterMorphModal open={taskId !== undefined} onOpenChange={changeOpen}>
      <CenterMorphModalContent
        ariaLabel="Task details"
        showCloseButton={false}
        className="max-w-[860px]"
      >
        {shownTaskId && (
          <TaskDetail
            key={shownTaskId}
            taskId={shownTaskId}
            live={taskId === shownTaskId}
            onDeleted={onDeleted}
          />
        )}
      </CenterMorphModalContent>
    </CenterMorphModal>
  );
}

function TaskDetail({
  taskId,
  live,
  onDeleted,
}: {
  taskId: string;
  live: boolean;
  onDeleted?: (taskId: string) => void;
}) {
  const navigate = useNavigate();
  const [task, setTask] = useState<DownloadTask | null>(null);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  useEffect(() => {
    if (!live) return;
    let active = true;
    async function load() {
      try {
        const item = await api<DownloadTask>(`/tasks/${taskId}`);
        if (active) {
          setTask(item);
          setError("");
        }
      } catch (cause) {
        if (active)
          setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 1000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [live, taskId]);

  const close = () => navigate("/tasks", { replace: true });

  async function control(
    action: "pause" | "cancel" | "start",
  ): Promise<boolean> {
    setActionBusy(true);
    setError("");
    try {
      setTask(
        await api<DownloadTask>(`/tasks/${taskId}/${action}`, {
          method: "POST",
        }),
      );
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setActionBusy(false);
    }
  }

  async function resume(scheduledAt?: string): Promise<boolean> {
    setActionBusy(true);
    setError("");
    try {
      setTask(
        await api<DownloadTask>(`/tasks/${taskId}/resume`, {
          method: "POST",
          body: JSON.stringify(scheduledAt ? { scheduledAt } : {}),
        }),
      );
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setActionBusy(false);
    }
  }

  async function reschedule(scheduledAt: string): Promise<boolean> {
    setActionBusy(true);
    setError("");
    try {
      setTask(
        await api<DownloadTask>(`/tasks/${taskId}/schedule`, {
          method: "PUT",
          body: JSON.stringify({ scheduledAt }),
        }),
      );
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setActionBusy(false);
    }
  }

  async function deleteTask(deleteArtifact: boolean): Promise<boolean> {
    if (!task) return false;
    setActionBusy(true);
    setError("");
    try {
      const query = deleteArtifact ? "?deleteArtifact=true" : "";
      await api<void>(`/tasks/${taskId}${query}`, { method: "DELETE" });
      onDeleted?.(taskId);
      navigate("/tasks", { replace: true });
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setActionBusy(false);
    }
  }

  if (error && !task) {
    return (
      <ModalFrame title="Task unavailable" onClose={close}>
        <ErrorBox>{error}</ErrorBox>
      </ModalFrame>
    );
  }
  if (!task) {
    return (
      <ModalFrame title="Loading task…" onClose={close}>
        <Loading className="min-h-[180px]">
          Reading the latest task state…
        </Loading>
      </ModalFrame>
    );
  }

  const isVerifying = task.status === "verifying";
  const canPause =
    task.status === "queued" ||
    task.status === "resolving" ||
    task.status === "downloading" ||
    isVerifying;
  const canCancel =
    canPause ||
    task.status === "scheduled" ||
    task.status === "paused" ||
    task.status === "awaiting_confirmation";
  const canDelete =
    task.status === "completed" ||
    task.status === "failed" ||
    task.status === "cancelled";
  const activity = taskStepProgress(task);
  const isVerificationActivity = activity.step === "verifying";
  const eta =
    task.status === "scheduled" && task.scheduledAt
      ? `Starts ${new Date(task.scheduledAt).toLocaleString()}`
      : task.status === "paused"
        ? "Paused"
        : task.status === "completed" || task.status === "awaiting_confirmation"
          ? "Done"
          : task.status === "failed" || task.status === "cancelled"
            ? "—"
            : formatDuration(
                isVerificationActivity
                  ? task.verificationEtaSeconds
                  : task.etaSeconds,
              );
  const steps = taskSteps(task);
  const route =
    task.mirrorUrl || task.disableMirror || task.disableProxy
      ? [
          task.mirrorUrl ? `Temporary mirror: ${task.mirrorUrl}` : null,
          task.disableMirror ? "Mirror bypassed" : null,
          task.disableProxy ? "Proxy bypassed" : null,
        ]
          .filter(Boolean)
          .join(" · ")
      : "Server defaults";
  const sourceUrl = sourceModelUrl(
    task.provider,
    task.sourceId,
    task.resolvedRevision ?? task.requestedRevision,
  );

  return (
    <ModalFrame
      title={task.sourceId}
      titleUrl={sourceUrl}
      eyebrow={task.provider}
      status={task.status}
      statusLabel={
        task.status === "downloading" && activity.step === "processing"
          ? "Local processing"
          : undefined
      }
      onClose={close}
      footer={
        (canCancel || canDelete) && (
          <>
            {canPause && (
              <Button
                variant="outline"
                disabled={actionBusy}
                onClick={() => void control("pause")}
              >
                Pause
              </Button>
            )}
            {task.status === "scheduled" && task.scheduledAt && (
              <ScheduleControl
                scheduledAt={task.scheduledAt}
                disabled={actionBusy}
                onSave={reschedule}
              />
            )}
            {task.status === "scheduled" && (
              <Button
                disabled={actionBusy}
                onClick={() => void control("start")}
              >
                Start now
              </Button>
            )}
            {(task.status === "paused" || task.status === "failed") && (
              <ResumeControl
                retry={task.status === "failed"}
                disabled={actionBusy}
                onResume={resume}
              />
            )}
            {canCancel && (
              <DeleteConfirm
                triggerLabel="Cancel task"
                triggerVariant="outline"
                triggerSize="md"
                side="top"
                title="Cancel this task?"
                description="The task will stop and its downloaded staging files will be permanently removed."
                confirmLabel="Cancel task"
                busyLabel="Cancelling…"
                disabled={actionBusy}
                onConfirm={() => control("cancel")}
              />
            )}
            {canDelete && (
              <DeleteConfirm
                triggerLabel="Delete task"
                triggerVariant="outline"
                triggerSize="md"
                side="top"
                title={`Delete ${task.status} task?`}
                description={
                  task.status === "completed"
                    ? (deleteArtifact) =>
                        deleteArtifact
                          ? `The task record and published artifact will be removed. Artifact logical size: ${formatBytes(task.artifactTotalBytes ?? 0)}. Shared hardlinks can make the physical space released smaller.`
                          : "The task record and any retained staging data will be removed. Its published artifact and model files will remain on the shelf."
                    : "The task record and any downloaded staging data will be permanently removed."
                }
                optionLabel={
                  task.status === "completed"
                    ? "Also delete the published artifact and model files"
                    : undefined
                }
                confirmLabel="Delete task"
                disabled={actionBusy}
                onConfirm={deleteTask}
              />
            )}
          </>
        )
      }
    >
      <TaskSteps steps={steps} />

      <Panel className="grid gap-3 rounded-3xl p-4.5">
        <div className="grid gap-1 sm:flex sm:items-baseline sm:justify-between sm:gap-4">
          <span className={labelText}>{activity.label}</span>
          <strong className="text-lg font-semibold tracking-tight">
            {activity.value}
          </strong>
        </div>
        {activity.showBar && (
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-3.5">
            <ProgressBar
              size="lg"
              indeterminate={activity.indeterminate}
              percent={activity.percent}
              label={
                activity.percent === undefined
                  ? `${activity.label}, in progress`
                  : `${activity.label}, ${activity.percent}% complete`
              }
            />
            <strong className="min-w-[46px] whitespace-nowrap text-right text-base font-semibold tabular-nums text-primary">
              {activity.percent === undefined ? "—" : `${activity.percent}%`}
            </strong>
          </div>
        )}
        {activity.step === "processing" && (
          <p className="text-sm leading-relaxed text-muted-foreground">
            Network transfer is complete. Git LFS is finishing local file
            checkout and index refresh before verification. Git does not report
            a reliable percentage or ETA for this stage.
          </p>
        )}
        {(activity.step === "downloading" ||
          activity.step === "verifying" ||
          task.status === "completed") && (
          <div className="grid gap-2.5 sm:grid-cols-3">
            <Metric
              label={
                isVerificationActivity ? "Verification speed" : "Instant speed"
              }
              value={formatRate(
                isVerificationActivity
                  ? task.verificationInstantaneousBytesPerSecond
                  : task.instantaneousBytesPerSecond,
              )}
            />
            <Metric
              label={
                isVerificationActivity
                  ? "Average verification speed"
                  : "Average speed"
              }
              value={formatRate(
                isVerificationActivity
                  ? task.verificationAverageBytesPerSecond
                  : task.averageBytesPerSecond,
              )}
            />
            <Metric label="ETA" value={eta} />
          </div>
        )}
      </Panel>

      <section className="grid gap-2.5 sm:grid-cols-2">
        <Detail
          label="Requested revision"
          value={task.requestedRevision}
          mono
        />
        <Detail
          label="Resolved revision"
          value={task.resolvedRevision ?? "Pending resolution"}
          mono
        />
        {task.artifactAlias && (
          <Detail label="Artifact alias" value={task.artifactAlias} />
        )}
        {task.artifactTotalBytes !== undefined && (
          <Detail
            label="Artifact logical size"
            value={formatBytes(task.artifactTotalBytes)}
          />
        )}
        {task.totalBytes !== undefined && (
          <Detail
            label="Network transfer"
            value={`${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}`}
          />
        )}
        {!!task.reusedBytes && (
          <Detail
            label="Reused from shelf"
            value={`${formatBytes(task.reusedBytes)} · ${(task.reusedFileCount ?? 0).toLocaleString()} files`}
          />
        )}
        {!!task.hardlinkBytes && (
          <Detail
            label="Hardlink reuse"
            value={`${formatBytes(task.hardlinkBytes)} · ${(task.hardlinkFileCount ?? 0).toLocaleString()} files`}
          />
        )}
        {!!task.reflinkBytes && (
          <Detail
            label="Reflink fallback"
            value={`${formatBytes(task.reflinkBytes)} · ${(task.reflinkFileCount ?? 0).toLocaleString()} files`}
          />
        )}
        {!!task.copyBytes && (
          <Detail
            label="Local copy fallback"
            value={`${formatBytes(task.copyBytes)} · ${(task.copyFileCount ?? 0).toLocaleString()} files`}
          />
        )}
        <Detail
          label="Created"
          value={new Date(task.createdAt).toLocaleString()}
        />
        {task.status === "scheduled" && task.scheduledAt && (
          <Detail
            label="Scheduled start"
            value={new Date(task.scheduledAt).toLocaleString()}
          />
        )}
        <Detail label="Network route" value={route} />
      </section>

      {task.selectedPaths && (
        <FileTree
          title="Selected source files"
          files={task.selectedPaths.map((path) => ({ path }))}
        />
      )}

      {task.error && <ErrorBox>{task.error}</ErrorBox>}
      {task.status === "failed" && (
        <p className="text-sm leading-relaxed text-muted-foreground">
          Available downloaded data is retained. Retry continues using the
          locked revision. Delete task permanently removes the retained data.
        </p>
      )}
      {error && <ErrorBox>{error}</ErrorBox>}
      {task.artifactId && (
        <Notice tone="success">
          <span className={labelText}>Published artifact</span>
          <code className="break-all font-mono text-xs">{task.artifactId}</code>
        </Notice>
      )}
      {task.status === "awaiting_confirmation" && (
        <Confirmation task={task} onConfirmed={setTask} />
      )}
    </ModalFrame>
  );
}

const stepTone: Record<
  TaskStepView["state"],
  { marker: string; text: string }
> = {
  pending: {
    marker: "border-border bg-background text-muted-foreground",
    text: "text-muted-foreground",
  },
  complete: {
    marker: "border-primary bg-primary text-primary-foreground",
    text: "text-foreground",
  },
  active: {
    marker: "border-primary bg-primary/10 text-primary ring-4 ring-primary/10",
    text: "text-primary",
  },
  paused: {
    marker:
      "border-amber-500 bg-amber-500/10 text-amber-600 dark:text-amber-400",
    text: "text-amber-600 dark:text-amber-400",
  },
  waiting: {
    marker:
      "border-violet-500 bg-violet-500/10 text-violet-600 dark:text-violet-400",
    text: "text-violet-600 dark:text-violet-400",
  },
  failed: {
    marker: "border-destructive bg-destructive/10 text-destructive",
    text: "text-destructive",
  },
  cancelled: {
    marker: "border-muted-foreground bg-muted text-muted-foreground",
    text: "text-muted-foreground",
  },
};

function TaskSteps({ steps }: { steps: TaskStepView[] }) {
  return (
    <ol className="flex items-start px-1 pt-0.5" aria-label="Ingestion steps">
      {steps.map((step, index) => {
        const tone = stepTone[step.state];
        return (
          <li
            className={cn(
              "relative grid min-w-0 flex-1 basis-0 justify-items-center gap-2",
              tone.text,
            )}
            key={step.key}
            aria-current={
              step.state !== "pending" && step.state !== "complete"
                ? "step"
                : undefined
            }
          >
            {index > 0 && (
              <span
                aria-hidden
                className="absolute right-1/2 top-3 h-0.5 w-full overflow-hidden bg-border"
              >
                <span
                  className={cn(
                    "block h-full origin-left bg-primary/50 transition-transform duration-500 ease-[var(--ease-out)]",
                    step.state === "pending" && "scale-x-0",
                  )}
                />
              </span>
            )}
            <span
              className={cn(
                "relative z-10 grid size-[26px] place-items-center rounded-full border-2 text-[11px] font-semibold transition-colors duration-300",
                tone.marker,
              )}
              aria-hidden
            >
              {step.state === "complete" ? (
                <Check className="size-3" strokeWidth={3} />
              ) : step.state === "active" ? (
                <span className="size-1.5 animate-pulse rounded-full bg-primary" />
              ) : (
                index + 1
              )}
            </span>
            <span className="max-w-full truncate text-xs font-medium">
              {step.label}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function ModalFrame({
  title,
  titleUrl,
  eyebrow,
  status,
  statusLabel,
  onClose,
  footer,
  children,
}: {
  title: string;
  titleUrl?: string;
  eyebrow?: string;
  status?: string;
  statusLabel?: string;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="flex max-h-[calc(100dvh-6rem)] flex-col">
      <header className="flex items-start justify-between gap-3 border-b border-border px-4.5 pb-4.5 pt-5 sm:gap-6 sm:px-6">
        <div className="min-w-0">
          {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
          <h2 className="max-w-[650px] text-xl font-semibold leading-tight tracking-tight [overflow-wrap:anywhere] sm:text-2xl">
            {titleUrl ? (
              <a
                className="group transition-colors hover:text-primary"
                href={titleUrl}
                target="_blank"
                rel="noreferrer"
                title="Open model page at source"
              >
                {title}
                <ExternalLink
                  className="ml-2 inline size-4 align-[0.05em] text-muted-foreground transition-colors group-hover:text-primary"
                  aria-hidden
                />
              </a>
            ) : (
              title
            )}
          </h2>
        </div>
        <div className="flex shrink-0 items-center gap-2.5">
          {status && (
            <span className="hidden sm:block">
              <StatusBadge status={status} label={statusLabel} size="md" />
            </span>
          )}
          <Button
            variant="ghost"
            size="icon"
            aria-label="Close task details"
            onClick={onClose}
          >
            <X className="size-4" aria-hidden />
          </Button>
        </div>
      </header>
      <div className="grid min-h-0 gap-4 overflow-y-auto px-4.5 pb-5 pt-4 sm:px-6 sm:pb-6 sm:pt-5.5">
        {children}
      </div>
      {footer && (
        <footer className="flex shrink-0 flex-wrap justify-end gap-2.5 border-t border-border bg-card px-4.5 py-3.5 sm:px-6">
          {footer}
        </footer>
      )}
    </div>
  );
}

function Confirmation({
  task,
  onConfirmed,
}: {
  task: DownloadTask;
  onConfirmed: (task: DownloadTask) => void;
}) {
  const metadata = task.inferredMetadata!;
  const [name, setName] = useState(metadata.name);
  const [version, setVersion] = useState(metadata.version);
  const [format, setFormat] = useState(metadata.format ?? "");
  const [extract, setExtract] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function confirm(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      onConfirmed(
        await api<DownloadTask>(`/tasks/${task.id}/confirm`, {
          method: "POST",
          body: JSON.stringify({
            name,
            version,
            format: format || null,
            extract,
          }),
        }),
      );
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="grid gap-5 rounded-3xl border border-border bg-card p-5 sm:p-7"
      onSubmit={(event) => void confirm(event)}
    >
      <div>
        <Eyebrow>Human checkpoint</Eyebrow>
        <h2 className="mb-1.5 text-xl font-semibold tracking-tight">
          Review before publication
        </h2>
        <p className="leading-relaxed text-muted-foreground">
          Confidence: {metadata.confidence}. Nothing has been extracted or
          exposed yet.
        </p>
      </div>
      {metadata.notes.length > 0 && (
        <ul className="list-disc rounded-2xl border border-border bg-background py-3.5 pl-8 pr-3.5 text-[13px] leading-relaxed text-muted-foreground">
          {metadata.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        <Input label="Name" value={name} onChange={setName} />
        <Input label="Version" value={version} onChange={setVersion} />
      </div>
      <Input
        label="Format"
        value={format}
        placeholder="Optional"
        onChange={setFormat}
      />
      {metadata.archive && (
        <CheckRow
          checked={extract}
          onCheckedChange={setExtract}
          title="Extract the archive"
          description="Paths and member types are validated before extraction."
        />
      )}
      {error && <ErrorBox>{error}</ErrorBox>}
      <StatefulButton
        type="submit"
        size="lg"
        state={busy ? "loading" : "idle"}
        loadingText="Publishing…"
        disabled={!name || !version}
      >
        Confirm and publish
      </StatefulButton>
    </form>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <Inset className="grid gap-1 px-3.5 py-2.5">
      <span className={labelText}>{label}</span>
      <strong className="whitespace-nowrap text-sm font-semibold tabular-nums">
        {value}
      </strong>
    </Inset>
  );
}

function Detail({
  label,
  value,
  mono = false,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <MetaTile label={label} mono={mono}>
      {value}
    </MetaTile>
  );
}
