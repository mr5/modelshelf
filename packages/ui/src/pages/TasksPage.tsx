import {
  ArrowRight,
  ChevronLeft,
  ChevronRight,
  GripVertical,
  Plus,
  Search,
  Trash2,
} from "lucide-react";
import { type ReactNode, useEffect, useRef, useState } from "react";
import type { DragEvent } from "react";
import {
  Link,
  useNavigate,
  useParams,
  useSearchParams,
} from "react-router-dom";
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
  const [searchParams, setSearchParams] = useSearchParams();
  const [loading, setLoading] = useState(true);
  const [tasks, setTasks] = useState<DownloadTask[]>([]);
  const [limits, setLimits] = useState<ServerInfo["downloads"]>();
  const [tasksError, setTasksError] = useState("");
  const [infoError, setInfoError] = useState("");
  const query = searchParams.get("q") ?? "";
  const provider =
    sourceOptions.find(
      (option) => option.value === searchParams.get("provider"),
    )?.value ?? "";
  const statusFilter =
    statusOptions.find(
      (option) => option.value === searchParams.get("statusFilter"),
    )?.value ?? "active-paused";
  const requestedOffset = Number(searchParams.get("offset"));
  const offset = Number.isFinite(requestedOffset)
    ? Math.max(0, Math.floor(requestedOffset / pageSize) * pageSize)
    : 0;
  const [total, setTotal] = useState(0);
  const queueRef = useRef<HTMLDivElement>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string>();
  const [draggedTaskId, setDraggedTaskId] = useState<string>();
  const [dropTarget, setDropTarget] = useState<{
    id: string;
    after: boolean;
  }>();
  const [reorderingQueue, setReorderingQueue] = useState(false);
  const [queueError, setQueueError] = useState("");
  const layout = useQueueLayout();
  const queueEmpty = !loading && tasks.length === 0;

  function updateFilters(values: {
    q?: string;
    provider?: Provider | "";
    statusFilter?: StatusFilter;
    offset?: number;
  }) {
    setSearchParams(
      (current) => {
        const next = new URLSearchParams(current);
        for (const [key, value] of Object.entries(values)) {
          if (
            value === "" ||
            value === 0 ||
            (key === "statusFilter" && value === "active-paused")
          )
            next.delete(key);
          else next.set(key, String(value));
        }
        return next;
      },
      { replace: true },
    );
  }
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

  const renderQueue = (task: DownloadTask) => (
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
              if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
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
  );

  const renderStatus = (task: DownloadTask) => (
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
              Verify {formatRate(task.verificationInstantaneousBytesPerSecond)}
            </strong>
            <span aria-hidden>·</span>
            <span>ETA {formatDuration(task.verificationEtaSeconds)}</span>
          </LiveMetrics>
        ))}
    </div>
  );

  const columns: TableColumn<DownloadTask>[] = [
    {
      key: "queue",
      header: "Queue",
      width: "96px",
      cell: renderQueue,
    },
    {
      key: "sourceId",
      header: "Source",
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
          className="min-w-0"
        >
          <Link
            className={`block font-semibold transition-colors hover:text-primary ${layout === "compact" ? "[overflow-wrap:anywhere]" : "truncate"}`}
            to={{
              pathname: `/tasks/${task.id}`,
              search: searchParams.toString(),
            }}
            title={task.sourceId}
          >
            {task.sourceId}
          </Link>
          <span className="mt-1 block text-xs text-muted-foreground">
            {task.provider}
          </span>
          {layout !== "wide" && (
            <>
              <div className="mt-1">
                <TaskRevision task={task} />
              </div>
              <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                <span>Updated </span>
                <time dateTime={task.updatedAt}>
                  {new Date(task.updatedAt).toLocaleString()}
                </time>
              </p>
            </>
          )}
          {layout === "compact" && (
            <div className="mt-3 grid gap-3 whitespace-normal">
              {renderStatus(task)}
              <TaskProgress task={task} />
            </div>
          )}
          {layout !== "wide" && sortableQueueStatuses.has(task.status) && (
            <div className="mt-2">{renderQueue(task)}</div>
          )}
        </div>
      ),
    },
    {
      key: "status",
      header: "Status",
      width: "208px",
      cell: renderStatus,
    },
    {
      key: "revision",
      header: "Revision",
      width: "136px",
      cell: (task) => <TaskRevision task={task} />,
    },
    {
      key: "progress",
      header: "Progress",
      width: "184px",
      cell: (task) => <TaskProgress task={task} />,
    },
    {
      key: "updatedAt",
      header: "Updated",
      width: "144px",
      cell: (task) => (
        <time
          dateTime={task.updatedAt}
          className="block text-xs leading-relaxed text-muted-foreground"
        >
          <span className="block">
            {new Date(task.updatedAt).toLocaleDateString()}
          </span>
          <span className="block">
            {new Date(task.updatedAt).toLocaleTimeString()}
          </span>
        </time>
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
          onChange={(value) => updateFilters({ q: value, offset: 0 })}
        />
        <SelectField
          label="Source"
          value={provider}
          options={sourceOptions}
          onChange={(value) => updateFilters({ provider: value, offset: 0 })}
        />
        <SelectField
          label="Status"
          value={statusFilter}
          options={statusOptions}
          onChange={(value) =>
            updateFilters({ statusFilter: value, offset: 0 })
          }
        />
      </div>
      <div
        ref={queueRef}
        role="region"
        aria-label="Download queue"
        className="scroll-mt-20 overflow-hidden rounded-3xl border border-border bg-background"
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
          columns={columns.filter(
            (column) =>
              layout === "wide" ||
              column.key === "sourceId" ||
              column.key === "actions" ||
              (layout === "medium" &&
                ["status", "progress"].includes(column.key)),
          )}
          getRowId={(task) => task.id}
          rowHeight={88}
          scrollMode="page"
          loading={loading}
          emptyState={null}
          className={`border-0 [&_thead_tr]:h-12! [&_th>div]:h-12! [&_th]:static [&_td]:py-4 [&_td]:whitespace-normal [&_tr:has([data-drop=before])>td]:shadow-[inset_0_3px_0_var(--primary)] [&_tr:has([data-drop=after])>td]:shadow-[inset_0_-3px_0_var(--primary)] ${queueEmpty ? "[&_tbody]:hidden" : ""}`}
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
            onClick={() => {
              updateFilters({ offset: Math.max(0, offset - pageSize) });
              queueRef.current?.scrollIntoView({ block: "start" });
            }}
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
            onClick={() => {
              updateFilters({ offset: offset + pageSize });
              queueRef.current?.scrollIntoView({ block: "start" });
            }}
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

function TaskRevision({ task }: { task: DownloadTask }) {
  const revision = task.resolvedRevision ?? task.requestedRevision;
  if (task.status === "completed" && task.artifactId) {
    return (
      <Link
        to={`/artifacts/${encodeURIComponent(task.artifactId)}`}
        aria-label={`View published artifact for ${task.sourceId}, revision ${revision}`}
        title={`View published artifact: ${revision}`}
        className="flex min-w-0 items-center gap-1.5 rounded-sm font-mono text-xs text-primary underline decoration-primary/40 underline-offset-4 transition-colors hover:decoration-primary focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring"
      >
        <span className="min-w-0 truncate" title={revision}>
          {revision}
        </span>
        <ArrowRight className="size-3.5 shrink-0" aria-hidden />
      </Link>
    );
  }
  return (
    <span
      className="block truncate font-mono text-xs text-muted-foreground"
      title={revision}
    >
      {revision}
    </span>
  );
}

function LiveMetrics({ children }: { children: ReactNode }) {
  return (
    <span className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1 whitespace-normal text-xs leading-tight text-muted-foreground">
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
      <span className="mt-1 block whitespace-normal text-xs text-muted-foreground">
        {activity.value}
      </span>
    </div>
  );
}

// The queue is paginated, so it can fit the page instead of adding a scroll viewport.
function useQueueLayout() {
  function currentLayout() {
    return window.matchMedia("(min-width: 1280px)").matches
      ? ("wide" as const)
      : window.matchMedia("(min-width: 768px)").matches
        ? ("medium" as const)
        : ("compact" as const);
  }
  const [layout, setLayout] = useState(currentLayout);
  useEffect(() => {
    const queries = [
      window.matchMedia("(min-width: 1280px)"),
      window.matchMedia("(min-width: 768px)"),
    ];
    const update = () => setLayout(currentLayout());
    queries.forEach((query) => query.addEventListener("change", update));
    return () =>
      queries.forEach((query) => query.removeEventListener("change", update));
  }, []);
  return layout;
}
