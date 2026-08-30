import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import { FileTree } from "../components/FileTree.tsx";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import { ResumeControl } from "../components/ResumeControl.tsx";
import { ScheduleControl } from "../components/ScheduleControl.tsx";
import { sourceModelUrl } from "../source.ts";
import { taskStepProgress, taskSteps, type TaskStepView } from "../taskProgress.ts";
import type { DownloadTask } from "../types.ts";

export function TaskPage({ taskId, onDeleted }: { taskId: string; onDeleted?: (taskId: string) => void }) {
  const navigate = useNavigate();
  const [task, setTask] = useState<DownloadTask | null>(null);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const item = await api<DownloadTask>(`/tasks/${taskId}`);
        if (active) {
          setTask(item);
          setError("");
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 1000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [taskId]);

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") navigate("/tasks", { replace: true });
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [navigate]);

  const close = () => navigate("/tasks", { replace: true });

  async function control(action: "pause" | "cancel" | "start"): Promise<boolean> {
    setActionBusy(true);
    setError("");
    try {
      setTask(await api<DownloadTask>(`/tasks/${taskId}/${action}`, { method: "POST" }));
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
      setTask(await api<DownloadTask>(`/tasks/${taskId}/resume`, {
        method: "POST",
        body: JSON.stringify(scheduledAt ? { scheduledAt } : {}),
      }));
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
      setTask(await api<DownloadTask>(`/tasks/${taskId}/schedule`, {
        method: "PUT",
        body: JSON.stringify({ scheduledAt }),
      }));
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
    return <ModalFrame title="Task unavailable" onClose={close}><div className="error-box">{error}</div></ModalFrame>;
  }
  if (!task) {
    return <ModalFrame title="Loading task…" onClose={close}><div className="modal-loading">Reading the latest task state…</div></ModalFrame>;
  }

  const isVerifying = task.status === "verifying";
  const canPause = task.status === "queued" || task.status === "resolving" || task.status === "downloading" || isVerifying;
  const canCancel = canPause || task.status === "scheduled" || task.status === "paused" || task.status === "awaiting_confirmation";
  const canDelete = task.status === "completed" || task.status === "failed" || task.status === "cancelled";
  const activity = taskStepProgress(task);
  const isVerificationActivity = activity.step === "verifying";
  const eta = task.status === "scheduled" && task.scheduledAt
    ? `Starts ${new Date(task.scheduledAt).toLocaleString()}`
    : task.status === "paused"
    ? "Paused"
    : task.status === "completed" || task.status === "awaiting_confirmation"
      ? "Done"
      : task.status === "failed" || task.status === "cancelled"
        ? "—"
        : formatDuration(isVerificationActivity ? task.verificationEtaSeconds : task.etaSeconds);
  const steps = taskSteps(task);
  const route = task.mirrorUrl || task.disableMirror || task.disableProxy ? [
    task.mirrorUrl ? `Temporary mirror: ${task.mirrorUrl}` : null,
    task.disableMirror ? "Mirror bypassed" : null,
    task.disableProxy ? "Proxy bypassed" : null,
  ].filter(Boolean).join(" · ") : "Server defaults";
  const sourceUrl = sourceModelUrl(
    task.provider,
    task.sourceId,
    task.resolvedRevision ?? task.requestedRevision,
  );

  return <ModalFrame
    title={task.sourceId}
    titleUrl={sourceUrl}
    eyebrow={task.provider}
    status={task.status}
    onClose={close}
    footer={(canCancel || canDelete) && <>
      {canPause && <button className="ghost" disabled={actionBusy} onClick={() => void control("pause")}>Pause</button>}
      {task.status === "scheduled" && task.scheduledAt && <ScheduleControl scheduledAt={task.scheduledAt} disabled={actionBusy} onSave={reschedule} />}
      {task.status === "scheduled" && <button disabled={actionBusy} onClick={() => void control("start")}>Start now</button>}
      {task.status === "paused" && <ResumeControl disabled={actionBusy} onResume={resume} />}
      {canCancel && <DeleteConfirm
        triggerLabel="Cancel task"
        triggerClassName="danger"
        title="Cancel this task?"
        description="The task will stop and its downloaded staging files will be permanently removed."
        confirmLabel="Cancel task"
        busyLabel="Cancelling…"
        disabled={actionBusy}
        onConfirm={() => control("cancel")}
      />}
      {canDelete && <DeleteConfirm
        triggerLabel="Delete task"
        triggerClassName="danger"
        title={`Delete ${task.status} task?`}
        description={task.status === "completed"
          ? (deleteArtifact) => deleteArtifact
            ? `The task record and published artifact will be removed. Artifact logical size: ${formatBytes(task.artifactTotalBytes ?? 0)}. Shared hardlinks can make the physical space released smaller.`
            : "The task record and any retained staging data will be removed. Its published artifact and model files will remain on the shelf."
          : "The task record and any downloaded staging data will be permanently removed."}
        optionLabel={task.status === "completed" ? "Also delete the published artifact and model files" : undefined}
        confirmLabel="Delete task"
        disabled={actionBusy}
        onConfirm={deleteTask}
      />}
    </>}
  >
    <TaskSteps steps={steps} />

    <section className="task-progress-card">
      <div className="task-progress-summary"><span>{activity.label}</span><strong>{activity.value}</strong></div>
      {activity.showBar && <div className="task-progress-row">
        <div
          className={`progress big ${activity.indeterminate ? "indeterminate" : ""}`}
          aria-label={activity.percent === undefined ? `${activity.label}, in progress` : `${activity.label}, ${activity.percent}% complete`}
        ><i style={{ width: `${activity.percent ?? 0}%` }} /></div>
        <strong className="task-progress-percent">{activity.percent === undefined ? "—" : `${activity.percent}%`}</strong>
      </div>}
      {(activity.step === "downloading" || activity.step === "verifying" || task.status === "completed") && <div className="task-metrics">
        <Metric label={isVerificationActivity ? "Verification speed" : "Instant speed"} value={formatRate(isVerificationActivity ? task.verificationInstantaneousBytesPerSecond : task.instantaneousBytesPerSecond)} />
        <Metric label={isVerificationActivity ? "Average verification speed" : "Average speed"} value={formatRate(isVerificationActivity ? task.verificationAverageBytesPerSecond : task.averageBytesPerSecond)} />
        <Metric label="ETA" value={eta} />
      </div>}
    </section>

    <section className="task-meta-grid">
      <Detail label="Requested revision" value={task.requestedRevision} mono />
      <Detail label="Resolved revision" value={task.resolvedRevision ?? "Pending resolution"} mono />
      {task.artifactAlias && <Detail label="Artifact alias" value={task.artifactAlias} />}
      {task.artifactTotalBytes !== undefined && <Detail label="Artifact logical size" value={formatBytes(task.artifactTotalBytes)} />}
      {task.totalBytes !== undefined && <Detail label="Network transfer" value={`${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}`} />}
      {!!task.reusedBytes && <Detail label="Reused from shelf" value={`${formatBytes(task.reusedBytes)} · ${(task.reusedFileCount ?? 0).toLocaleString()} files`} />}
      {!!task.hardlinkBytes && <Detail label="Hardlink reuse" value={`${formatBytes(task.hardlinkBytes)} · ${(task.hardlinkFileCount ?? 0).toLocaleString()} files`} />}
      {!!task.reflinkBytes && <Detail label="Reflink fallback" value={`${formatBytes(task.reflinkBytes)} · ${(task.reflinkFileCount ?? 0).toLocaleString()} files`} />}
      {!!task.copyBytes && <Detail label="Local copy fallback" value={`${formatBytes(task.copyBytes)} · ${(task.copyFileCount ?? 0).toLocaleString()} files`} />}
      <Detail label="Created" value={new Date(task.createdAt).toLocaleString()} />
      {task.status === "scheduled" && task.scheduledAt && <Detail label="Scheduled start" value={new Date(task.scheduledAt).toLocaleString()} />}
      <Detail label="Network route" value={route} />
    </section>

    {task.selectedPaths && <FileTree title="Selected source files" files={task.selectedPaths.map((path) => ({ path }))} />}

    {task.error && <div className="error-box">{task.error}</div>}
    {error && <div className="error-box">{error}</div>}
    {task.artifactId && <div className="task-published"><span>Published artifact</span><code>{task.artifactId}</code></div>}
    {task.status === "awaiting_confirmation" && <Confirmation task={task} onConfirmed={setTask} />}
  </ModalFrame>;
}

function TaskSteps({ steps }: { steps: TaskStepView[] }) {
  return <ol className="task-steps" aria-label="Ingestion steps">
    {steps.map((step, index) => <li className={`task-step ${step.state}`} key={step.key} aria-current={step.state !== "pending" && step.state !== "complete" ? "step" : undefined}>
      <span className="task-step-marker" aria-hidden="true">{step.state === "complete" ? "✓" : index + 1}</span>
      <span className="task-step-label">{step.label}</span>
    </li>)}
  </ol>;
}

function ModalFrame({ title, titleUrl, eyebrow, status, onClose, footer, children }: {
  title: string;
  titleUrl?: string;
  eyebrow?: string;
  status?: string;
  onClose: () => void;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return <div className="modal-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="task-modal" role="dialog" aria-modal="true" aria-labelledby="task-modal-title">
      <header className="task-modal-header">
        <div className="task-modal-heading">
          {eyebrow && <p className="eyebrow">{eyebrow}</p>}
          <h2 id="task-modal-title">{titleUrl
            ? <a className="task-source-link" href={titleUrl} target="_blank" rel="noreferrer" title="Open model page at source">{title}<span aria-hidden="true">↗</span></a>
            : title}</h2>
        </div>
        <div className="task-modal-header-actions">
          {status && <span className={`badge large ${status}`}>{status.replace("_", " ")}</span>}
          <button className="modal-close" aria-label="Close task details" autoFocus onClick={onClose}>×</button>
        </div>
      </header>
      <div className="task-modal-body">{children}</div>
      {footer && <footer className="task-modal-footer">{footer}</footer>}
    </section>
  </div>;
}

function Confirmation({ task, onConfirmed }: { task: DownloadTask; onConfirmed: (task: DownloadTask) => void }) {
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
      onConfirmed(await api<DownloadTask>(`/tasks/${task.id}/confirm`, {
        method: "POST",
        body: JSON.stringify({ name, version, format: format || null, extract }),
      }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setBusy(false);
    }
  }

  return <form className="form-panel confirm modal-confirmation" onSubmit={(event) => void confirm(event)}>
    <div><p className="eyebrow">Human checkpoint</p><h2>Review before publication</h2><p className="muted">Confidence: {metadata.confidence}. Nothing has been extracted or exposed yet.</p></div>
    {metadata.notes.length > 0 && <ul className="notes">{metadata.notes.map((note) => <li key={note}>{note}</li>)}</ul>}
    <div className="two-col"><label>Name<input value={name} onChange={(event) => setName(event.target.value)} /></label><label>Version<input value={version} onChange={(event) => setVersion(event.target.value)} /></label></div>
    <label>Format<input value={format} placeholder="Optional" onChange={(event) => setFormat(event.target.value)} /></label>
    {metadata.archive && <label className="checkbox"><input type="checkbox" checked={extract} onChange={(event) => setExtract(event.target.checked)} /><span><strong>Extract the archive</strong><small>Paths and member types are validated before extraction.</small></span></label>}
    {error && <div className="error-box">{error}</div>}
    <button disabled={busy || !name || !version}>{busy ? "Publishing…" : "Confirm and publish"}</button>
  </form>;
}

function Metric({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function Detail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="task-meta"><span>{label}</span><strong className={mono ? "mono" : ""}>{value}</strong></div>;
}
