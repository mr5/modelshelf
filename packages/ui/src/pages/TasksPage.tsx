import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import type { DownloadTask, ServerInfo } from "../types.ts";
import { TaskPage } from "./TaskPage.tsx";

export function TasksPage() {
  const { id: selectedTaskId } = useParams();
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [limits, setLimits] = useState<ServerInfo["downloads"]>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    void api<ServerInfo>("/info")
      .then((info) => { if (active) setLimits(info.downloads); })
      .catch(() => undefined);
    async function load() {
      try {
        const result = await api<DownloadTask[]>("/tasks");
        if (active) setTasks(result);
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : String(cause));
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 1500);
    return () => { active = false; window.clearInterval(timer); };
  }, []);
  return (
    <div className="page">
      <div className="page-head">
        <div><p className="eyebrow">Ingestion queue</p><h1>Downloads</h1><p className="muted">Every request resolves to an immutable revision before publication.{limits && ` Up to ${limits.maxConcurrent} tasks run at once, with ${limits.maxConcurrentPerSource} per source.`}</p></div>
        <Link className="button" to="/tasks/new">New download</Link>
      </div>
      {error && <div className="error-box">{error}</div>}
      <section className="panel table-panel">
        <table className="tasks-table">
          <colgroup><col className="source-column" /><col className="status-column" /><col className="revision-column" /><col className="progress-column" /><col className="updated-column" /></colgroup>
          <thead><tr><th>Source</th><th>Status</th><th>Revision</th><th>Progress</th><th>Updated</th></tr></thead>
          <tbody>{tasks.map((task) => <tr key={task.id}>
            <td><Link className="row-title" to={`/tasks/${task.id}`}>{task.sourceId}</Link><span className="subline">{task.provider}</span></td>
            <td className="task-status-cell">
              <div className="task-status-stack">
                <span className={`badge ${task.status}`}>{task.status.replace("_", " ")}</span>
                {task.status === "downloading" && <span className="task-live-metrics"><strong>{formatRate(task.instantaneousBytesPerSecond)}</strong><span aria-hidden="true">·</span><span>ETA {formatDuration(task.etaSeconds)}</span></span>}
              </div>
            </td>
            <td><span className="mono truncate" title={task.resolvedRevision ?? task.requestedRevision}>{task.resolvedRevision ?? task.requestedRevision}</span></td>
            <td className="task-progress-cell"><div className="progress" aria-label={`${task.progress}% complete`}><span style={{ width: `${task.progress}%` }} /></div><span className="subline">{task.totalBytes ? `${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}` : `${formatBytes(task.bytesDownloaded)} transferred`}</span></td>
            <td className="task-updated">{new Date(task.updatedAt).toLocaleString()}</td>
          </tr>)}</tbody>
        </table>
        {tasks.length === 0 && <div className="empty"><h2>The shelf is quiet</h2><p>Create a download to ingest the first immutable artifact.</p></div>}
      </section>
      {selectedTaskId && <TaskPage taskId={selectedTaskId} />}
    </div>
  );
}
