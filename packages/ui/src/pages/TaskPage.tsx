import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import { sourceModelUrl } from "../source.ts";
import type { DownloadTask } from "../types.ts";

export function TaskPage({ taskId }: { taskId: string }) {
  const navigate = useNavigate();
  const [task, setTask] = useState<DownloadTask | null>(null);
  const [error, setError] = useState("");
  const [actionBusy, setActionBusy] = useState(false);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const item = await api<DownloadTask>(`/tasks/${taskId}`);
        if (active) setTask(item);
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

  async function control(action: "pause" | "resume" | "cancel") {
    if (action === "cancel" && !window.confirm("Cancel this task and delete its staged files?")) {
      return;
    }
    setActionBusy(true);
    setError("");
    try {
      setTask(await api<DownloadTask>(`/tasks/${taskId}/${action}`, { method: "POST" }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
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

  const canPause = task.status === "queued" || task.status === "resolving" || task.status === "downloading";
  const canCancel = canPause || task.status === "paused" || task.status === "awaiting_confirmation";
  const eta = task.status === "paused"
    ? "Paused"
    : task.status === "completed" || task.status === "awaiting_confirmation"
      ? "Done"
      : task.status === "failed" || task.status === "cancelled"
        ? "—"
        : formatDuration(task.etaSeconds);
  const transferred = task.totalBytes
    ? `${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}`
    : `${formatBytes(task.bytesDownloaded)} transferred`;
  const route = task.disableMirror || task.disableProxy ? [
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
    footer={canCancel && <>
      {canPause && <button className="ghost" disabled={actionBusy} onClick={() => void control("pause")}>Pause</button>}
      {task.status === "paused" && <button disabled={actionBusy} onClick={() => void control("resume")}>Resume</button>}
      <button className="danger" disabled={actionBusy} onClick={() => void control("cancel")}>Cancel task</button>
    </>}
  >
    <section className="task-progress-card">
      <div className="task-progress-summary"><span>Transferred</span><strong>{transferred}</strong></div>
      <div className="task-progress-row">
        <div className="progress big" aria-label={`${task.progress}% complete`}><i style={{ width: `${task.progress}%` }} /></div>
        <strong className="task-progress-percent">{task.progress}%</strong>
      </div>
      <div className="task-metrics">
        <Metric label="Instant speed" value={formatRate(task.instantaneousBytesPerSecond)} />
        <Metric label="Average speed" value={formatRate(task.averageBytesPerSecond)} />
        <Metric label="ETA" value={eta} />
      </div>
    </section>

    <section className="task-meta-grid">
      <Detail label="Requested revision" value={task.requestedRevision} mono />
      <Detail label="Resolved revision" value={task.resolvedRevision ?? "Pending resolution"} mono />
      <Detail label="Created" value={new Date(task.createdAt).toLocaleString()} />
      <Detail label="Network route" value={route} />
    </section>

    {task.error && <div className="error-box">{task.error}</div>}
    {error && <div className="error-box">{error}</div>}
    {task.artifactId && <div className="task-published"><span>Published artifact</span><code>{task.artifactId}</code></div>}
    {task.status === "awaiting_confirmation" && <Confirmation task={task} onConfirmed={setTask} />}
  </ModalFrame>;
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
