import {
  ChevronLeft,
  ChevronRight,
  GripVertical,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import type { DragEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Button } from "@/components/motion/button/base";
import { Table, type TableColumn } from "@/components/motion/table";
import { Input } from "@/components/motion/input";
import {
  EmptyState,
  ErrorBox,
  PageHeader,
  PageShell,
  ProgressBar,
  SelectField,
  StatusBadge,
  statusText,
} from "@/components/ui";
import { api, formatBytes, formatDuration, formatRate } from "../api.ts";
import { DeleteConfirm } from "../components/DeleteConfirm.tsx";
import {
  currentTaskStep,
  localProcessingElapsed,
  taskStepProgress,
} from "../taskProgress.ts";
import type {
  DownloadTask,
  Page,
  Provider,
  ServerInfo,
  TaskStatus,
} from "../types.ts";
import { TaskPage } from "./TaskPage.tsx";

const terminalStatuses = new Set<TaskStatus>([
  "completed",
  "failed",
  "cancelled",
]);
const sortableQueueStatuses = new Set<TaskStatus>(["queued", "paused"]);
const pageSize = 50;

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

const sourceOptions: Array<{ value: Provider | ""; label: string }> = [
  { value: "", label: "All sources" },
  ...providers,
];

const statusOptions: Array<{ value: StatusFilter; label: string }> = [
  { value: "active-paused", label: "Active & paused" },
  { value: "all", label: "All statuses" },
  { value: "active", label: "Active (all in-progress phases)" },
  { value: "scheduled", label: "Scheduled" },
  { value: "downloading", label: "Downloading" },
  { value: "paused", label: "Paused" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "queued", label: "Queued" },
  { value: "awaiting_confirmation", label: "Awaiting confirmation" },
  { value: "cancelled", label: "Cancelled" },
  { value: "resolving", label: "Resolving" },
  { value: "verifying", label: "Verifying" },
  { value: "publishing", label: "Publishing" },
];

export function TasksPage() {
  const { id: selectedTaskId } = useParams();
  const navigate = useNavigate();
  const [loading, setLoading] = useState(true);
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [limits, setLimits] = useState<ServerInfo["downloads"]>();
  const [tasksError, setTasksError] = useState("");
  const [infoError, setInfoError] = useState("");
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState<Provider | "">("");
  const [statusFilter, setStatusFilter] =
    useState<StatusFilter>("active-paused");
  const [offset, setOffset] = useState(0);
  const [total, setTotal] = useState(0);
  const [deletingTaskId, setDeletingTaskId] = useState<string>();
  const [draggedTaskId, setDraggedTaskId] = useState<string>();
  const [dropTarget, setDropTarget] = useState<{
    id: string;
    after: boolean;
  }>();
  const [reorderingQueue, setReorderingQueue] = useState(false);
  const [queueError, setQueueError] = useState("");
  const queueEmpty = !loading && tasks.length === 0;
  useEffect(() => {
    let active = true;
    setLoading(true);
    void api<ServerInfo>("/info")
      .then((info) => {
        if (active) setLimits(info.downloads);
      })
      .catch((cause) => {
        if (active)
          setInfoError(
            `Could not load download limits: ${cause instanceof Error ? cause.message : String(cause)}`,
          );
      });
    async function load() {
      try {
        const parameters = new URLSearchParams({
          limit: String(pageSize),
          offset: String(offset),
          statusFilter,
        });
        if (query.trim()) parameters.set("q", query.trim());
        if (provider) parameters.set("provider", provider);
        const result = await api<Page<DownloadTask>>(
          `/tasks/page?${parameters.toString()}`,
        );
        if (active) {
          setTasks(result.items);
          setTotal(result.total);
          setTasksError("");
        }
      } catch (cause) {
        if (active)
          setTasksError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        if (active) setLoading(false);
      }
    }
    void load();
    const timer = window.setInterval(() => void load(), 1500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [offset, provider, query, statusFilter]);

  async function deleteTask(
    task: DownloadTask,
    deleteArtifact: boolean,
  ): Promise<boolean> {
    setDeletingTaskId(task.id);
    setTasksError("");
    try {
      const query = deleteArtifact ? "?deleteArtifact=true" : "";
      await api<void>(`/tasks/${task.id}${query}`, { method: "DELETE" });
      setTasks((current) => current.filter((item) => item.id !== task.id));
      setTotal((current) => Math.max(0, current - 1));
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
    setTotal((current) => Math.max(0, current - 1));
  }

  function beginQueueDrag(event: DragEvent<HTMLButtonElement>, taskId: string) {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", taskId);
    setDraggedTaskId(taskId);
    setQueueError("");
  }

  function markQueueDrop(
    event: DragEvent<HTMLDivElement>,
    taskId: string,
    row: HTMLTableRowElement,
  ) {
    if (!draggedTaskId || draggedTaskId === taskId || reorderingQueue) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const bounds = row.getBoundingClientRect();
    setDropTarget({
      id: taskId,
      after: event.clientY >= bounds.top + bounds.height / 2,
    });
  }

  async function reorderQueue(
    targetId: string,
    after: boolean,
    movingId = draggedTaskId,
  ) {
    if (!movingId || movingId === targetId || reorderingQueue) return;
    const queued = tasks
      .filter((task) => sortableQueueStatuses.has(task.status))
      .sort(
        (left, right) =>
          (left.queuePosition ?? Number.MAX_SAFE_INTEGER) -
            (right.queuePosition ?? Number.MAX_SAFE_INTEGER) ||
          left.createdAt.localeCompare(right.createdAt),
      );
    const draggedIndex = queued.findIndex((task) => task.id === movingId);
    if (draggedIndex < 0 || !queued.some((task) => task.id === targetId))
      return;
    setReorderingQueue(true);
    setQueueError("");
    try {
      const reordered = await api<DownloadTask[]>(
        `/tasks/${movingId}/position`,
        {
          method: "POST",
          body: JSON.stringify({ targetTaskId: targetId, after }),
        },
      );
      const saved = new Map(reordered.map((task) => [task.id, task]));
      setTasks((current) => current.map((task) => saved.get(task.id) ?? task));
    } catch (cause) {
      setQueueError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setReorderingQueue(false);
      setDraggedTaskId(undefined);
      setDropTarget(undefined);
    }
  }

  const columns: TableColumn<DownloadTask>[] = [
    {
      key: "queue",
      header: "Queue",
      width: "100px",
      cell: (task) => (
        <div
          data-task-id={task.id}
          data-drop={
            dropTarget?.id === task.id
              ? dropTarget.after
                ? "after"
                : "before"
              : undefined
          }
          className="flex items-center gap-1 text-xs font-medium tabular-nums text-muted-foreground"
        >
          {sortableQueueStatuses.has(task.status) && (
            <>
              <Button
                variant="ghost"
                size="icon"
                pressScale={1}
                className="shrink-0 cursor-grab active:cursor-grabbing"
                draggable={!reorderingQueue}
                disabled={reorderingQueue}
                aria-label={`Move ${task.sourceId}, priority ${(task.queuePosition ?? -1) + 1}`}
                title="Drag to change priority, or focus and press ↑ / ↓. Paused tasks stay paused."
                onDragStartCapture={(event: DragEvent<HTMLButtonElement>) =>
                  beginQueueDrag(event, task.id)
                }
                onDragEndCapture={() => {
                  setDraggedTaskId(undefined);
                  setDropTarget(undefined);
                }}
                onKeyDown={(event) => {
                  if (event.key !== "ArrowUp" && event.key !== "ArrowDown")
                    return;
                  event.preventDefault();
                  const queued = tasks
                    .filter((item) => sortableQueueStatuses.has(item.status))
                    .sort(
                      (left, right) =>
                        (left.queuePosition ?? Number.MAX_SAFE_INTEGER) -
                          (right.queuePosition ?? Number.MAX_SAFE_INTEGER) ||
                        left.createdAt.localeCompare(right.createdAt),
                    );
                  const index = queued.findIndex((item) => item.id === task.id);
                  const after = event.key === "ArrowDown";
                  const adjacent = queued[index + (after ? 1 : -1)];
                  if (adjacent) void reorderQueue(adjacent.id, after, task.id);
                }}
              >
                <GripVertical className="size-4" aria-hidden />
              </Button>
              <span>#{(task.queuePosition ?? -1) + 1}</span>
            </>
          )}
        </div>
      ),
    },
    {
      key: "sourceId",
      header: "Source",
      width: "220px",
      cell: (task) => (
        <div>
          <Link
            className="block truncate font-semibold transition-colors hover:text-primary"
            to={`/tasks/${task.id}`}
          >
            {task.sourceId}
          </Link>
          <span className="mt-1 block text-xs text-muted-foreground">
            {task.provider}
          </span>
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      width: "220px",
      cell: (task) => (
        <div className="grid justify-items-start gap-2">
          <StatusBadge
            status={task.status}
            label={
              task.status === "downloading" &&
              currentTaskStep(task) === "processing"
                ? statusText("local_processing")
                : undefined
            }
          />
          {task.status === "scheduled" && task.scheduledAt && (
            <LiveMetrics>
              Starts {new Date(task.scheduledAt).toLocaleString()}
            </LiveMetrics>
          )}
          {task.status === "downloading" &&
            (currentTaskStep(task) === "processing" ? (
              <LiveMetrics>{localProcessingElapsed(task)}</LiveMetrics>
            ) : (
              <LiveMetrics>
                <strong className="text-xs font-semibold text-foreground">
                  {formatRate(task.instantaneousBytesPerSecond)}
                </strong>
                <span aria-hidden>·</span>
                <span>ETA {formatDuration(task.etaSeconds)}</span>
              </LiveMetrics>
            ))}
          {task.status === "verifying" &&
            (task.verificationTotalBytes === undefined ||
            task.verificationDetail === "Waiting for verification capacity" ? (
              <LiveMetrics>
                {task.verificationDetail ?? "Verification in progress"}
              </LiveMetrics>
            ) : (
              <LiveMetrics>
                <strong className="text-xs font-semibold text-foreground">
                  Verify{" "}
                  {formatRate(task.verificationInstantaneousBytesPerSecond)}
                </strong>
                <span aria-hidden>·</span>
                <span>ETA {formatDuration(task.verificationEtaSeconds)}</span>
              </LiveMetrics>
            ))}
        </div>
      ),
    },
    {
      key: "revision",
      header: "Revision",
      width: "160px",
      cell: (task) => (
        <span
          className="block truncate font-mono text-xs"
          title={task.resolvedRevision ?? task.requestedRevision}
        >
          {task.resolvedRevision ?? task.requestedRevision}
        </span>
      ),
    },
    {
      key: "progress",
      header: "Progress",
      width: "200px",
      cell: (task) => <TaskProgress task={task} />,
    },
    {
      key: "updatedAt",
      header: "Updated",
      width: "180px",
      cell: (task) => (
        <span className="whitespace-normal text-xs text-muted-foreground">
          {new Date(task.updatedAt).toLocaleString()}
        </span>
      ),
    },
    {
      key: "actions",
      header: <span className="sr-only">Actions</span>,
      width: "64px",
      align: "right",
      cell: (task) =>
        terminalStatuses.has(task.status) && (
          <DeleteConfirm
            triggerLabel="Delete task"
            triggerIcon={<Trash2 className="size-4" aria-hidden />}
            triggerSize="icon"
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
            disabled={deletingTaskId === task.id}
            onConfirm={(deleteArtifact) => deleteTask(task, deleteArtifact)}
          />
        ),
    },
  ];

  function queueRow(target: EventTarget) {
    if (!(target instanceof Element)) return;
    const row = target.closest("tbody tr");
    const taskId =
      row?.querySelector<HTMLElement>("[data-task-id]")?.dataset.taskId;
    const task = tasks.find((item) => item.id === taskId);
    if (
      row instanceof HTMLTableRowElement &&
      task &&
      sortableQueueStatuses.has(task.status)
    )
      return { row, task };
  }

  return (
    <PageShell>
      <PageHeader
        eyebrow="Ingestion queue"
        title="Downloads"
        description={
          <>
            Every request resolves to an immutable revision before publication.
            {limits &&
              ` Up to ${limits.maxConcurrent} tasks run at once, with ${limits.maxConcurrentPerSource} per source.`}{" "}
            Drag queued or paused tasks to set their priority. Paused tasks
            remain paused.
          </>
        }
        actions={
          <Button onClick={() => navigate("/tasks/new")}>
            <Plus className="size-4" aria-hidden />
            New download
          </Button>
        }
      />
      <div className="mb-4 grid gap-3 empty:hidden">
        {infoError && <ErrorBox>{infoError}</ErrorBox>}
        {tasksError && (
          <ErrorBox>Could not refresh downloads: {tasksError}</ErrorBox>
        )}
        {queueError && (
          <ErrorBox>Could not reorder downloads: {queueError}</ErrorBox>
        )}
      </div>
      <div className="relative z-20 mb-4 grid items-end gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(260px,1fr)_220px_240px]">
        <Input
          className="sm:col-span-2 lg:col-span-1"
          aria-label="Search downloads"
          placeholder="Search model ID or revision…"
          leftIcon={<Search />}
          value={query}
          onChange={(value) => {
            setQuery(value);
            setOffset(0);
          }}
        />
        <SelectField
          label="Source"
          value={provider}
          options={sourceOptions}
          onChange={(value) => {
            setProvider(value);
            setOffset(0);
          }}
        />
        <SelectField
          label="Status"
          value={statusFilter}
          options={statusOptions}
          onChange={(value) => {
            setStatusFilter(value);
            setOffset(0);
          }}
        />
      </div>
      <div
        role="region"
        aria-label="Download queue"
        className="overflow-hidden rounded-3xl border border-border bg-background"
        onDragOver={(event) => {
          const target = queueRow(event.target);
          if (target) markQueueDrop(event, target.task.id, target.row);
        }}
        onDrop={(event) => {
          const target = queueRow(event.target);
          if (!target) return;
          event.preventDefault();
          const bounds = target.row.getBoundingClientRect();
          void reorderQueue(
            target.task.id,
            event.clientY >= bounds.top + bounds.height / 2,
          );
        }}
      >
        <Table
          data={tasks}
          columns={columns}
          getRowId={(task) => task.id}
          rowHeight={88}
          height={Math.min(560, Math.max(200, (tasks.length + 1) * 88))}
          loading={loading}
          emptyState={null}
          className={`border-0 [&_tr:has([data-drop=before])>td]:shadow-[inset_0_3px_0_var(--primary)] [&_tr:has([data-drop=after])>td]:shadow-[inset_0_-3px_0_var(--primary)] ${queueEmpty ? "[&_tbody]:hidden [&>div]:h-auto!" : ""}`}
        />
        {queueEmpty && (
          <EmptyState title="No matching downloads">
            Change the keyword, source or status filters, or create a new
            download.
          </EmptyState>
        )}
      </div>
      {total > pageSize && (
        <div className="flex items-center justify-center gap-3 pt-6">
          <Button
            variant="outline"
            size="sm"
            disabled={offset === 0}
            onClick={() => setOffset(Math.max(0, offset - pageSize))}
          >
            <ChevronLeft className="size-3.5" aria-hidden />
            Previous
          </Button>
          <span className="text-[13px] tabular-nums text-muted-foreground">
            {offset + 1}–{Math.min(offset + tasks.length, total)} of {total}
          </span>
          <Button
            variant="outline"
            size="sm"
            disabled={offset + tasks.length >= total}
            onClick={() => setOffset(offset + pageSize)}
          >
            Next
            <ChevronRight className="size-3.5" aria-hidden />
          </Button>
        </div>
      )}
      <TaskPage taskId={selectedTaskId} onDeleted={taskDeleted} />
    </PageShell>
  );
}

function LiveMetrics({ children }: { children: ReactNode }) {
  return (
    <span className="flex items-baseline gap-1.5 text-xs leading-tight text-muted-foreground">
      {children}
    </span>
  );
}

function TaskProgress({ task }: { task: DownloadTask }) {
  const activity = taskStepProgress(task);
  return (
    <div>
      {activity.showBar ? (
        <ProgressBar
          className="max-w-[230px]"
          indeterminate={activity.indeterminate}
          percent={activity.percent}
          label={
            activity.percent === undefined
              ? `${activity.label}, in progress`
              : `${activity.label}, ${activity.percent}% complete`
          }
        />
      ) : (
        <span className="text-xs font-semibold">{activity.label}</span>
      )}
      <span className="mt-1 block whitespace-nowrap text-xs text-muted-foreground">
        {activity.value}
      </span>
    </div>
  );
}
