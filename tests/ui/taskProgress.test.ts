import assert from "node:assert/strict";
import { test } from "node:test";
import { currentTaskStep, localProcessingElapsed, taskStepProgress, taskSteps } from "../../packages/ui/src/taskProgress.ts";
import type { DownloadTask } from "../../packages/ui/src/types.ts";

const task: DownloadTask = {
  id: "test", provider: "modelscope-cn", sourceId: "owner/model",
  requestedRevision: "master", status: "downloading", progress: 89,
  bytesDownloaded: 1000, totalBytes: 1000,
  createdAt: "2026-09-21T00:00:00Z", updatedAt: "2026-09-21T00:00:20Z",
  downloadElapsedSeconds: 20, localProcessingStartedAfterSeconds: 5,
};

test("completed ModelScope transfer has a separate indeterminate processing step", () => {
  for (const provider of ["modelscope-cn", "modelscope-ai"] as const) {
    const item = { ...task, provider };
    assert.equal(currentTaskStep(item), "processing");
    const progress = taskStepProgress(item);
    assert.equal(progress.indeterminate, true);
    assert.equal(progress.percent, undefined);
    assert.match(progress.value, /15s elapsed/);
    assert.deepEqual(taskSteps(item).map(({ key, state }) => [key, state]), [
      ["resolving", "complete"], ["downloading", "complete"], ["processing", "active"],
      ["verifying", "pending"], ["publishing", "pending"],
    ]);
  }
});

test("old records show the phase without inventing elapsed time", () => {
  const item = { ...task, localProcessingStartedAfterSeconds: undefined };
  assert.equal(currentTaskStep(item), "processing");
  assert.equal(localProcessingElapsed(item), "Elapsed time unavailable");
  assert.equal(localProcessingElapsed({ ...task, downloadElapsedSeconds: 5 }), "0s elapsed");
});

test("partial, unknown and empty transfers do not enter local processing", () => {
  for (const item of [
    { ...task, bytesDownloaded: 999 }, { ...task, totalBytes: undefined },
    { ...task, bytesDownloaded: 0, totalBytes: 0 },
    { ...task, provider: "huggingface" as const },
    { ...task, provider: "http" as const },
  ]) assert.equal(currentTaskStep(item), "downloading");
});

test("stopped processing never animates and later phases take precedence", () => {
  for (const status of ["paused", "failed", "cancelled", "queued", "scheduled"] as const) {
    const item = { ...task, status, resumeFromStage: true };
    assert.equal(currentTaskStep(item), "processing");
    assert.equal(taskStepProgress(item).indeterminate, false);
  }
  for (const status of ["verifying", "publishing"] as const) {
    assert.equal(currentTaskStep({ ...task, status }), status);
  }
  assert.equal(currentTaskStep({ ...task, status: "paused", verificationTotalBytes: 1000 }), "verifying");
  assert.ok(taskSteps({ ...task, status: "completed" }).every(step => step.state === "complete"));
});
