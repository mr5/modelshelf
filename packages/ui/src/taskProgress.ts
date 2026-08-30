import { formatBytes } from "./api.ts";
import type { DownloadTask, TaskStatus } from "./types.ts";

export type TaskStepKey = "resolving" | "downloading" | "confirmation" | "verifying" | "publishing";
export type TaskStepState = "pending" | "active" | "complete" | "paused" | "failed" | "cancelled" | "waiting";

export interface TaskStepView {
  key: TaskStepKey;
  label: string;
  state: TaskStepState;
}

export interface TaskStepProgress {
  step?: TaskStepKey;
  label: string;
  value: string;
  percent?: number;
  indeterminate: boolean;
  showBar: boolean;
}

const stepLabels: Record<TaskStepKey, string> = {
  resolving: "Resolve",
  downloading: "Download",
  confirmation: "Confirm",
  verifying: "Verify",
  publishing: "Publish",
};

function stepKeys(task: DownloadTask): TaskStepKey[] {
  return task.provider === "http"
    ? ["resolving", "downloading", "confirmation", "verifying", "publishing"]
    : ["resolving", "downloading", "verifying", "publishing"];
}

function stoppedStep(task: DownloadTask): TaskStepKey {
  // `progress` used to encode whole-pipeline progress. It remains useful only to locate the
  // phase of stopped tasks created before step progress was separated in the UI.
  if (task.artifactId || task.progress >= 99) return "publishing";
  if (
    task.verificationTotalBytes !== undefined
    || (task.verificationBytesCompleted ?? 0) > 0
    || task.verificationDetail
  ) return "verifying";
  if (task.provider === "http" && task.inferredMetadata) return "confirmation";
  if (task.progress >= 92) return "verifying";
  if (task.resolvedRevision || task.bytesDownloaded > 0 || task.progress >= 2) return "downloading";
  return "resolving";
}

export function currentTaskStep(task: DownloadTask): TaskStepKey | undefined {
  switch (task.status) {
    case "resolving": return "resolving";
    case "downloading": return "downloading";
    case "awaiting_confirmation": return "confirmation";
    case "verifying": return "verifying";
    case "publishing": return "publishing";
    case "paused":
    case "failed":
    case "cancelled":
      return stoppedStep(task);
    case "scheduled":
    case "queued":
      return task.resumeFromStage ? stoppedStep(task) : undefined;
    case "completed":
      return undefined;
  }
}

function activeState(status: TaskStatus): TaskStepState {
  if (status === "paused") return "paused";
  if (status === "failed") return "failed";
  if (status === "cancelled") return "cancelled";
  if (status === "scheduled" || status === "queued" || status === "awaiting_confirmation") return "waiting";
  return "active";
}

export function taskSteps(task: DownloadTask): TaskStepView[] {
  const keys = stepKeys(task);
  const current = currentTaskStep(task);
  const currentIndex = current === undefined ? -1 : keys.indexOf(current);
  return keys.map((key, index) => ({
    key,
    label: stepLabels[key],
    state: task.status === "completed"
      ? "complete"
      : currentIndex < 0
        ? "pending"
        : index < currentIndex
          ? "complete"
          : index === currentIndex
            ? activeState(task.status)
            : "pending",
  }));
}

function percent(completed: number, total?: number): number | undefined {
  if (total === undefined || total <= 0) return undefined;
  return Math.max(0, Math.min(100, Math.floor(completed / total * 100)));
}

function transferred(task: DownloadTask): string {
  return task.totalBytes
    ? `${formatBytes(task.bytesDownloaded)} / ${formatBytes(task.totalBytes)}`
    : `${formatBytes(task.bytesDownloaded)} transferred`;
}

export function taskStepProgress(task: DownloadTask): TaskStepProgress {
  const step = currentTaskStep(task);
  if (step === "downloading") {
    const stepPercent = percent(task.bytesDownloaded, task.totalBytes);
    return {
      step,
      label: "Download progress",
      value: transferred(task),
      percent: stepPercent,
      indeterminate: stepPercent === undefined && task.status === "downloading",
      showBar: true,
    };
  }
  if (step === "verifying") {
    const completed = task.verificationBytesCompleted ?? 0;
    const total = task.verificationTotalBytes;
    const stepPercent = percent(completed, total);
    return {
      step,
      label: task.verificationDetail ?? "Verification progress",
      value: total === undefined
        ? (completed > 0 ? `${formatBytes(completed)} verified` : "Preparing verification…")
        : `${formatBytes(completed)} / ${formatBytes(total)}`,
      percent: stepPercent,
      indeterminate: stepPercent === undefined && task.status === "verifying",
      showBar: true,
    };
  }
  if (step === "resolving") {
    return {
      step,
      label: "Resolution",
      value: task.status === "paused" ? "Paused" : task.status === "failed" ? "Failed" : task.status === "cancelled" ? "Cancelled" : "Resolving immutable revision…",
      indeterminate: task.status === "resolving",
      showBar: task.status === "resolving",
    };
  }
  if (step === "confirmation") {
    return {
      step,
      label: "Confirmation",
      value: task.status === "cancelled" ? "Cancelled" : "Waiting for user confirmation",
      indeterminate: false,
      showBar: false,
    };
  }
  if (step === "publishing") {
    return {
      step,
      label: "Publication",
      value: task.status === "failed" ? "Failed" : task.status === "cancelled" ? "Cancelled" : "Publishing immutable artifact…",
      indeterminate: task.status === "publishing",
      showBar: task.status === "publishing",
    };
  }
  if (task.status === "completed") {
    return { label: "Transfer summary", value: transferred(task), indeterminate: false, showBar: false };
  }
  if (task.status === "scheduled") {
    return {
      label: "Scheduled",
      value: task.scheduledAt ? `Starts ${new Date(task.scheduledAt).toLocaleString()}` : "Waiting for its start time",
      indeterminate: false,
      showBar: false,
    };
  }
  return { label: "Queue", value: "Waiting for download capacity", indeterminate: false, showBar: false };
}
