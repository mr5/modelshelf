import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import type { DownloadTask, Provider, ServerInfo, TaskStatus } from "../types.ts";
import { TaskPage } from "./TaskPage.tsx";

const terminalStatuses = new Set<TaskStatus>(["completed", "failed", "cancelled"]);
const activeStatuses = new Set<TaskStatus>([
  "scheduled",
  "queued",
  "resolving",
  "downloading",
  "verifying",
  "awaiting_confirmation",
  "publishing",
]);

const providers: Array<{ value: Provider; label: string }> = [
  { value: "huggingface", label: "Hugging Face Hub" },
  { value: "modelscope-cn", label: "ModelScope CN" },
  { value: "modelscope-ai", label: "ModelScope AI" },
  { value: "github-release", label: "GitHub Releases" },
  { value: "kaggle", label: "Kaggle Models" },
  { value: "http", label: "Generic HTTP" },
  { value: "filesystem", label: "Filesystem import" },
];

type StatusFilter = "active-paused" | "active" | TaskStatus | "all";

export function TasksPage() {
  const { id: selectedTaskId } = useParams();
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [limits, setLimits] = useState<ServerInfo["downloads"]>();
  const [tasksError, setTasksError] = useState("");
  const [infoError, setInfoError] = useState("");
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<Provider | "">("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("active-paused");
  const [deletingTaskId, setDeletingTaskId] = useState<string>();
  useEffect(() => {
    let active = true;
    void api<ServerInfo>("/info")
      .then((info) => { if (active) setLimits(info.downloads); })
      .catch((cause) => {
        if (active) setInfoError(`Could not load download limits: ${cause instanceof Error ? cause.message : String(cause)}`);
      });
    async function load() {
      try {
        const result = await api<DownloadTask[]>("/tasks");
        if (active) {
          setTasks(result);
          setTasksError("");
        }
      } catch (cause) {
        if (active) setTasksError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  const filteredTasks = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return tasks.filter((task) => {
      if (provider && task.provider !== provider) return false;
      if (statusFilter === "active-paused") {
        if (!activeStatuses.has(task.status) && task.status !== "paused") return false;
      } else if (statusFilter === "active") {
        if (!activeStatuses.has(task.status)) return false;
      } else if (statusFilter !== "all" && task.status !== statusFilter) {
        return false;
      }
      if (!needle) return true;
      return [task.sourceId, task.provider, task.requestedRevision, task.resolvedRevision ?? ""]
        .some((value) => value.toLocaleLowerCase().includes(needle));
    });
  }, [provider, query, statusFilter, tasks]);

  async function deleteTask(task: DownloadTask, deleteArtifact: boolean): Promise<boolean> {
    setDeletingTaskId(task.id);
    setTasksError("");
    try {
      const query = deleteArtifact ? "?deleteArtifact=true" : "";
      await api<void>(`/tasks/${task.id}${query}`, { method: "DELETE" });
      setTasks((current) => current.filter((item) => item.id !== task.id));
      return true;
    } catch (cause) {
      setTasksError(cause instanceof Error ? cause.message : String(cause));
      return false;
    } finally {
      setDeletingTaskId(undefined);
    }
  }

  function taskDeleted(taskId: string) {
    setTasks((current) => current.filter((item) => item.id !== taskId));
  }

  return (
    <div className="page">
      <div className="page-head">
        <div><p className="eyebrow">Ingestion queue</p><h1>Downloads</h1><p className="muted">Every request resolves to an immutable revision before publication.{limits && ` Up to ${limits.maxConcurrent} tasks run at once, with ${limits.maxConcurrentPerSource} per source.`}</p></div>
        <Link className="button" to="/tasks/new">New download</Link>
      </div>
      {infoError && <div className="error-box">{infoError}</div>}
      {tasksError && <div className="error-box">Could not refresh downloads: {tasksError}</div>}
      <div className="task-filters">
        <input className="search" aria-label="Search downloads" placeholder="Search model ID or revision…" value={query} onChange={(event) => setQuery(event.target.value)} />
        <label><span>Source</span><select value={provider} onChange={(event) => setProvider(event.target.value as Provider | "")}>
          <option value="">All sources</option>
          {providers.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
        </select></label>
        <label><span>Status</span><select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value as StatusFilter)}>
          <option value="active-paused">Active &amp; paused</option>
          <option value="all">All statuses</option>
          <option value="active">Active (all in-progress phases)</option>
          <option value="scheduled">Scheduled</option>
          <option value="downloading">Downloading</option>
          <option value="paused">Paused</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="queued">Queued</option>
          <option value="awaiting_confirmation">Awaiting confirmation</option>
          <option value="cancelled">Cancelled</option>
          <option value="resolving">Resolving</option>
          <option value="verifying">Verifying</option>
          <option value="publishing">Publishing</option>
        </select></label>
      </div>
      <section className="panel table-panel">
        <table className="tasks-table">
          <colgroup><col className="source-column" /><col className="status-column" /><col className="revision-column" /><col className="progress-column" /><col className="updated-column" /><col className="actions-column" /></colgroup>
          <thead><tr><th>Source</th><th>Status</th><th>Revision</th><th>Progress</th><th>Updated</th><th><span className="sr-only">Actions</span></th></tr></thead>
          <tbody>{filteredTasks.map((task) => <tr key={task.id}>
            <td><Link className="row-title" to={`/tasks/${task.id}`}>{task.sourceId}</Link><span className="subline">{task.provider}</span></td>
            <td className="task-status-cell">
              <div className="task-status-stack">
                <span className={`badge ${task.status}`}>{task.status.replace("_", " ")}</span>
                {task.status === "scheduled" && task.scheduledAt && <span className="task-live-metrics"><span>Starts {new Date(task.scheduledAt).toLocaleString()}</span></span>}
                {task.status === "downloading" && <span className="task-live-metrics"><strong>{formatRate(task.instantaneousBytesPerSecond)}</strong><span aria-hidden="true">·</span><span>ETA {formatDuration(task.etaSeconds)}</span></span>}
              </div>
            </td>
            <td><span className="mono truncate" title={task.resolvedRevision ?? task.requestedRevision}>{task.resolvedRevision ?? task.requestedRevision}</span></td>
            <td className="task-progress-cell"><div className="progress" aria-label={`${task.progress}% complete`}><span style={{ width: `${task.progress}%` }} /></div><span className="subline">{task.totalBytes ? `${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}` : `${formatBytes(task.bytesDownloaded)} transferred`}</span></td>
            <td className="task-updated">{new Date(task.updatedAt).toLocaleString()}</td>
            <td className="task-row-actions">{terminalStatuses.has(task.status) && <DeleteConfirm
              triggerLabel="Delete"
              title={`Delete ${task.status} task?`}
              description={task.status === "completed"
                ? (deleteArtifact) => deleteArtifact
                  ? "The task record, retained staging data, published artifact manifest and all model files will be permanently removed."
                  : "The task record and any retained staging data will be removed. Its published artifact and model files will remain on the shelf."
                : "The task record and any downloaded staging data will be permanently removed."}
              optionLabel={task.status === "completed" ? "Also delete the published artifact and model files" : undefined}
              confirmLabel="Delete task"
              disabled={deletingTaskId === task.id}
              onConfirm={(deleteArtifact) => deleteTask(task, deleteArtifact)}
            />}</td>
          </tr>)}</tbody>
        </table>
        {tasks.length === 0 && <div className="empty"><h2>The shelf is quiet</h2><p>Create a download to ingest the first immutable artifact.</p></div>}
        {tasks.length > 0 && filteredTasks.length === 0 && <div className="empty"><h2>No matching downloads</h2><p>Change the keyword, source or status filters to see other tasks.</p></div>}
      </section>
      {selectedTaskId && <TaskPage taskId={selectedTaskId} onDeleted={taskDeleted} />}
    </div>
  );
}
