import { expect, test, type Page } from "@playwright/test";
import type { ArtifactDetail, DownloadTask } from "../src/types";

const task: DownloadTask = {
  id: "paused-task",
  provider: "huggingface",
  sourceId: "acme/model",
  requestedRevision: "main",
  resolvedRevision: "locked-commit",
  status: "paused",
  queuePosition: 0,
  progress: 0.5,
  bytesDownloaded: 512,
  totalBytes: 1024,
  createdAt: "2026-10-06T01:00:00Z",
  updatedAt: "2026-10-06T01:00:00Z",
};
const artifact: ArtifactDetail = {
  summary: {
    artifactId: "artifact-one",
    name: "Model One",
    version: "main",
    provider: "huggingface",
    sourceId: "acme/model",
    requestedRevision: "main",
    resolvedRevision: "locked-commit",
    totalSize: 1024,
    fileCount: 2,
    createdAt: task.createdAt,
    relativePath: "huggingface/acme/model",
  },
  manifest: {
    schemaVersion: 1,
    artifactId: "artifact-one",
    name: "Model One",
    version: "main",
    source: {
      provider: "huggingface",
      id: "acme/model",
      requestedRevision: "main",
      resolvedRevision: "locked-commit",
    },
    contentSha256: "hash",
    createdAt: task.createdAt,
    totalSize: 1024,
    fileCount: 2,
    files: [
      { path: "weights/model.bin", size: 1000, sha256: "hash" },
      { path: "config.json", size: 24, sha256: "hash" },
    ],
  },
};

async function mockShelf(page: Page) {
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace("/api/v1", "");
    const json =
      path === "/auth/session"
        ? { authenticated: true }
        : path === "/info"
          ? {
              name: "ModelShelf",
              version: "test",
              publicArtifacts: true,
              downloads: { maxConcurrent: 2, maxConcurrentPerSource: 1 },
              network: { mirrors: {}, proxyConfigured: false },
            }
          : path === "/tasks/page"
            ? {
                items: [
                  task,
                  {
                    ...task,
                    id: "queued-task",
                    sourceId: "acme/second",
                    status: "queued",
                    queuePosition: 1,
                  },
                ],
                total: 2,
                hasMore: false,
              }
            : path.endsWith("/models")
              ? { models: [{ id: "acme/model", name: "Model One" }] }
              : path.endsWith("/revisions")
                ? {
                    defaultRevision: "main",
                    revisions: [
                      { name: "main", kind: "branch" },
                      { name: "release", kind: "tag" },
                    ],
                  }
                : path.endsWith("/estimate")
                  ? {
                      downloadable: true,
                      totalSize: 1024,
                      fileCount: 2,
                      resolvedRevision: "locked-commit",
                      metadata: [],
                      storageSufficient: true,
                      fileSelectionAvailable: true,
                      selectableFiles: artifact.manifest.files,
                    }
                  : path === "/artifacts/page"
                    ? { items: [artifact.summary], total: 1, hasMore: false }
                    : path === "/artifacts/artifact-one"
                      ? artifact
                      : path.endsWith("/position")
                        ? [task]
                        : path.startsWith("/tasks/")
                          ? task
                          : {};
    await route.fulfill({ json });
  });
}

const runtimeErrors = new WeakMap<Page, string[]>();
test.beforeEach(async ({ page }) => {
  await mockShelf(page);
  const errors: string[] = [];
  runtimeErrors.set(page, errors);
  page.on("pageerror", (error) => errors.push(error.message));
});

test.afterEach(async ({ page }) => {
  expect(runtimeErrors.get(page), "No uncaught UI errors").toEqual([]);
});

test("editable model and revision suggestions preserve exact manual values", async ({
  page,
}) => {
  await page.goto("/tasks/new");
  const model = page.getByRole("combobox", { name: "Model ID", exact: true });
  await model.fill("acme");
  await page.getByRole("option", { name: "acme/model", exact: false }).click();
  await expect(model).toHaveValue("acme/model");
  const revision = page.getByRole("combobox", {
    name: "Requested revision",
    exact: true,
  });
  await revision.fill("custom-commit");
  await page.getByRole("heading", { name: "Download a model" }).click();
  await expect(revision).toHaveValue("custom-commit");
  await expect(
    page.getByRole("button", { name: "Start download", exact: true }),
  ).toBeEnabled();
  await model.fill("private/unlisted");
  await page.getByRole("heading", { name: "Download a model" }).click();
  await expect(model).toHaveValue("private/unlisted");
  await expect(revision).toHaveValue("main");
});

test("completed revisions open their published artifact on desktop and mobile", async ({
  page,
}, testInfo) => {
  const revision = "eed8d15085f0b0790d21f0c3cee774f234425390";
  await page.route("**/api/v1/tasks/page?*", (route) =>
    route.fulfill({
      json: {
        items: [
          {
            ...task,
            id: "completed-task",
            status: "completed",
            resolvedRevision: revision,
            artifactId: "artifact-one",
          },
          {
            ...task,
            id: "unlinked-task",
            sourceId: "acme/no-artifact",
            status: "completed",
            resolvedRevision: revision,
          },
          {
            ...task,
            id: "unfinished-task",
            sourceId: "acme/unfinished",
            artifactId: "artifact-one",
            resolvedRevision: revision,
          },
        ],
        total: 3,
        hasMore: false,
      },
    }),
  );
  const listUrl = "/tasks?statusFilter=all&provider=huggingface&q=acme";
  for (const width of [1440, 1024, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto(listUrl);
    const queue = page.getByRole("region", { name: "Download queue" });
    const link = queue.getByRole("link", {
      name: `View published artifact for acme/model, revision ${revision}`,
      exact: true,
    });
    await expect(link).toHaveAttribute("href", "/artifacts/artifact-one");
    await expect(link).toHaveText(revision);
    await expect(
      queue.getByRole("link", { name: /^View published artifact/ }),
    ).toHaveCount(1);
    for (const source of ["acme/no-artifact", "acme/unfinished"]) {
      const row = queue
        .locator("tbody tr")
        .filter({ has: page.getByRole("link", { name: source, exact: true }) });
      await expect(row.locator(`[title="${revision}"]`)).toHaveText(revision);
    }
    if (width === 1440)
      await page.screenshot({
        path: testInfo.outputPath("artifact-link-1440.png"),
      });
    if (width === 320) await link.press("Enter");
    else await link.click();
    await expect(page).toHaveURL(/\/artifacts\/artifact-one$/);
    const detail = page.getByRole("dialog", { name: "Artifact details" });
    await expect(detail).toBeVisible();
    await expect(
      detail.getByRole("heading", { name: "Model One", exact: true }),
    ).toBeVisible();
    if (width === 1440)
      await page.screenshot({
        path: testInfo.outputPath("artifact-details-1440.png"),
      });
    await page.goBack();
    await expect(page).toHaveURL(listUrl);
    await expect(link).toBeVisible();
    await expect(
      page.getByRole("textbox", { name: "Search downloads" }),
    ).toHaveValue("acme");
    await expect(
      page.getByRole("button", { name: "Status: All statuses", exact: true }),
    ).toBeVisible();
    await queue.getByRole("link", { name: "acme/model", exact: true }).click();
    await expect(page).toHaveURL(
      `/tasks/completed-task?statusFilter=all&provider=huggingface&q=acme`,
    );
    await page
      .getByRole("button", { name: "Close task details", exact: true })
      .click();
    await expect(page).toHaveURL(listUrl);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  }
});

for (const width of [1440, 1280, 1024, 768, 767, 390, 320]) {
  test(`long task lists fit and scroll with the document at ${width}px`, async ({
    page,
  }, testInfo) => {
    const revision = "eed8d15085f0b0790d21f0c3cee774f234425390";
    const statuses = [
      "completed",
      "awaiting_confirmation",
      "downloading",
      "failed",
      "paused",
      "verifying",
    ] as const;
    await page.route("**/api/v1/tasks/page?*", (route) => {
      const offset = Number(
        new URL(route.request().url()).searchParams.get("offset"),
      );
      return route.fulfill({
        json: {
          items: Array.from({ length: 50 }, (_, index) => ({
            ...task,
            id: `long-task-${offset + index}`,
            sourceId: `Comfy-Org/Qwen-Image-2.1-large-model-${offset + index}`,
            provider: "modelscope-cn",
            resolvedRevision: revision,
            status: statuses[index % statuses.length],
            totalBytes: 69 * 1024 ** 3,
            bytesDownloaded: 69 * 1024 ** 3,
          })),
          total: 100,
          hasMore: offset === 0,
        },
      });
    });
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/tasks");
    const queue = page.getByRole("region", { name: "Download queue" });
    const rows = queue.locator("tbody tr");
    await expect(rows).toHaveCount(50);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      `${width}px document fits`,
    ).toBe(true);
    expect(
      await queue.evaluate((element) =>
        [...element.querySelectorAll("*")].some((child) => {
          const style = getComputedStyle(child);
          return (
            (/auto|scroll/.test(style.overflowY) &&
              child.scrollHeight > child.clientHeight) ||
            (/auto|scroll/.test(style.overflowX) &&
              child.scrollWidth > child.clientWidth)
          );
        }),
      ),
      `${width}px queue has no scrollbars`,
    ).toBe(false);
    for (const row of [rows.nth(0), rows.nth(1), rows.nth(2)]) {
      const updated = row.locator("time");
      await expect(updated).toHaveCount(1);
      await expect(updated).toHaveAttribute("datetime", task.updatedAt);
      await expect(updated).toContainText("2026");
      const timeBox = await updated.boundingBox();
      const timeCell = await updated
        .locator("xpath=ancestor::td")
        .boundingBox();
      expect(timeBox!.x).toBeGreaterThanOrEqual(timeCell!.x);
      expect(timeBox!.x + timeBox!.width).toBeLessThanOrEqual(
        timeCell!.x + timeCell!.width - 14,
      );
      expect(timeBox!.y + timeBox!.height).toBeLessThanOrEqual(
        timeCell!.y + timeCell!.height - 14,
      );
      if (width < 1280)
        await expect(row.getByText("Updated", { exact: true })).toBeVisible();
      const badge = row.locator("[data-badge-label]");
      const bounds = await badge.boundingBox();
      const cell = await badge.locator("xpath=ancestor::td").boundingBox();
      expect(bounds!.x).toBeGreaterThanOrEqual(cell!.x);
      expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
        cell!.x + cell!.width - 14,
      );
      expect(
        await badge.evaluate(
          (element) => element.scrollWidth <= element.clientWidth,
        ),
      ).toBe(true);
    }
    const hash = rows.first().locator(`[title="${revision}"]`);
    await expect(hash).toHaveText(revision);
    expect(
      await hash.evaluate((element) => getComputedStyle(element).textOverflow),
    ).toBe("ellipsis");
    await rows.first().scrollIntoViewIfNeeded();
    const before = await page.evaluate(() => window.scrollY);
    await rows.first().hover();
    await page.mouse.wheel(0, 500);
    await expect
      .poll(() => page.evaluate(() => window.scrollY))
      .toBeGreaterThan(before);
    expect(
      await queue
        .locator("table")
        .locator("..")
        .evaluate((element) => element.scrollTop),
    ).toBe(0);
    await rows.last().scrollIntoViewIfNeeded();
    await expect(rows.last().getByRole("link")).toBeInViewport();
    await page.getByRole("button", { name: "Next", exact: true }).click();
    const nextFirst = rows.first().getByRole("link", {
      name: "Comfy-Org/Qwen-Image-2.1-large-model-50",
      exact: true,
    });
    await expect(nextFirst).toBeInViewport();
    await page.getByRole("button", { name: "Previous", exact: true }).click();
    await expect(
      rows.first().getByRole("link", {
        name: "Comfy-Org/Qwen-Image-2.1-large-model-0",
        exact: true,
      }),
    ).toBeInViewport();
    await page.evaluate(() => window.scrollTo(0, 0));
    if ([1440, 1024, 320].includes(width))
      await page.screenshot({
        path: testInfo.outputPath(`queue-${width}.png`),
      });
  });
}

test("empty download results show the full message below the table header", async ({
  page,
}) => {
  await page.route("**/api/v1/tasks/page?*", (route) => {
    const query = new URL(route.request().url()).searchParams.get("q");
    return route.fulfill({
      json: {
        items: query
          ? []
          : [
              task,
              ...Array.from({ length: 11 }, (_, index) => ({
                ...task,
                id: `task-${index}`,
                sourceId: `acme/other-${index}`,
              })),
            ],
        total: query ? 0 : 12,
        hasMore: false,
      },
    });
  });
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/tasks");
    await expect(
      page.getByRole("link", { name: "acme/model", exact: true }),
    ).toBeVisible();
    const search = page.getByRole("textbox", { name: "Search downloads" });
    await page
      .getByRole("region", { name: "Download queue" })
      .locator("tbody tr")
      .last()
      .scrollIntoViewIfNeeded();
    await search.fill("no-match");
    const queue = page.getByRole("region", { name: "Download queue" });
    const title = queue.getByRole("heading", {
      name: "No matching downloads",
      exact: true,
    });
    const message = queue.getByText(
      "Change the keyword, source or status filters, or create a new download.",
      { exact: true },
    );
    await expect(title).toBeVisible();
    await expect(message).toBeVisible();
    const header = await queue.locator("thead").boundingBox();
    const titleBox = await title.boundingBox();
    const messageBox = await message.boundingBox();
    const queueBox = await queue.boundingBox();
    expect(titleBox!.y).toBeGreaterThanOrEqual(header!.y + header!.height);
    expect(messageBox!.y + messageBox!.height).toBeLessThanOrEqual(
      queueBox!.y + queueBox!.height,
    );
    expect(titleBox!.x).toBeGreaterThanOrEqual(queueBox!.x);
    expect(messageBox!.x + messageBox!.width).toBeLessThanOrEqual(
      queueBox!.x + queueBox!.width,
    );
    const viewport = queue.locator("table").locator("..");
    expect(
      await viewport.evaluate(
        (element) => element.scrollHeight <= element.clientHeight,
      ),
    ).toBe(true);
    await search.clear();
    await expect(title).toHaveCount(0);
    await expect(
      page.getByRole("link", { name: "acme/model", exact: true }),
    ).toBeVisible();
  }
});

test("queue priority can be changed with a keyboard and retains paused status", async ({
  page,
}) => {
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/tasks");
    const handle = page.getByRole("button", {
      name: "Move acme/model, priority 1",
    });
    await handle.focus();
    const request = page.waitForRequest((request) =>
      request.url().endsWith("/paused-task/position"),
    );
    await handle.press("ArrowDown");
    expect((await request).postDataJSON()).toEqual({
      targetTaskId: "queued-task",
      after: true,
    });
    await expect(
      page.getByRole("link", { name: "acme/model", exact: true }),
    ).toBeVisible();
    await expect(
      page
        .getByRole("region", { name: "Download queue" })
        .getByText("Paused", { exact: true }),
    ).toBeVisible();
  }
});

test("revision suggestions can be browsed without clearing the current value", async ({
  page,
}) => {
  await page.goto("/tasks/new");
  await page
    .getByRole("combobox", { name: "Model ID", exact: true })
    .fill("acme/model");
  await page.getByRole("heading", { name: "Download a model" }).click();
  await expect(
    page.getByText("Hub revisions loaded.", { exact: false }),
  ).toBeVisible();
  const revision = page.getByRole("combobox", {
    name: "Requested revision",
    exact: true,
  });
  await revision.click();
  await expect(revision).toHaveValue("main");
  await expect(page.getByRole("option", { name: /release/ })).toBeVisible();
  await revision.press("ArrowDown");
  await revision.press("Enter");
  await expect(revision).toHaveValue("release");
  await revision.fill("custom-commit");
  await expect(page.getByRole("option", { name: /release/ })).toHaveCount(0);
  await page.getByRole("heading", { name: "Download a model" }).click();
  await revision.click();
  await expect(revision).toHaveValue("custom-commit");
  await expect(page.getByRole("option", { name: /release/ })).toBeVisible();
});

test("select fields support keyboard selection, dismissal and the page tab order", async ({
  page,
}) => {
  await page.goto("/tasks/new");
  const source = page
    .getByRole("group", { name: "Source", exact: true })
    .getByRole("button", { name: /^Source:/ });
  await source.focus();
  await source.press("ArrowDown");
  const selected = page.getByRole("option", {
    name: "Hugging Face Hub",
    exact: true,
  });
  await expect(selected).toBeFocused();
  await selected.press("ArrowDown");
  const next = page.getByRole("option", { name: "ModelScope CN", exact: true });
  await expect(next).toBeFocused();
  await next.press("Enter");
  await expect(source).toHaveText("Source:ModelScope CN");
  await expect(source).toBeFocused();
  await expect(source).toHaveAttribute("aria-expanded", "false");
  await source.press("ArrowDown");
  await page.keyboard.press("End");
  await expect(
    page.getByRole("option", { name: "Generic HTTP URL", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(source).toBeFocused();
  await source.press("ArrowDown");
  await page.keyboard.press("Tab");
  await expect(
    page.getByRole("combobox", { name: "Model ID", exact: true }),
  ).toBeFocused();
  await expect(source).toHaveAttribute("aria-expanded", "false");
});

test("source dropdown options remain clickable above adjacent inputs", async ({
  page,
}) => {
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 844 });
    await page.goto("/tasks/new");
    const source = page
      .getByRole("group", { name: "Source", exact: true })
      .getByRole("button", { name: /^Source:/ });
    await source.click();
    await page
      .getByRole("option", { name: "ModelScope AI", exact: true })
      .click();
    await expect(source).toHaveText("Source:ModelScope AI");
  }
});

test("moving from a long guide to another primary screen starts at the top", async ({
  page,
}) => {
  await page.route("**/integration.md", (route) =>
    route.fulfill({
      contentType: "text/markdown",
      body:
        "# Integration\n\nIntro\n\n" +
        "Paragraph with instructions.\n\n".repeat(150),
    }),
  );
  await page.goto("/integration");
  await expect(page.locator("article")).toBeVisible();
  await page.evaluate(() => window.scrollTo(0, 2000));
  expect(await page.evaluate(() => window.scrollY)).toBeGreaterThan(1000);
  await page.getByRole("link", { name: "Downloads", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Downloads", exact: true }),
  ).toBeInViewport();
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
});

test("small phones show readable artifact titles and the full advanced-options label", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await page.route("**/api/v1/artifacts/page?*", (route) =>
    route.fulfill({
      json: {
        items: [{ ...artifact.summary, name: "Demo embedding model" }],
        total: 1,
        hasMore: false,
      },
    }),
  );
  await page.goto("/artifacts");
  const title = page.getByRole("heading", {
    name: "Demo embedding model",
    exact: true,
  });
  await expect(title).toBeVisible();
  expect((await title.boundingBox())!.height).toBeLessThanOrEqual(48);
  await expect(
    page.getByRole("link", { name: "ModelShelf home" }),
  ).toBeVisible();
  await page.goto("/tasks/new");
  const advanced = page.getByRole("button", {
    name: /Advanced routing and delayed start/,
  });
  await advanced.scrollIntoViewIfNeeded();
  const description = advanced.getByText("routing and delayed start", {
    exact: true,
  });
  expect(
    await description.evaluate((element) => {
      const text = element.getBoundingClientRect();
      const button = element.closest("button")!.getBoundingClientRect();
      return text.right <= button.right && text.bottom <= button.bottom;
    }),
  ).toBe(true);
});

test("nested resume popover handles Escape before its task modal", async ({
  page,
}) => {
  await page.goto("/tasks/paused-task");
  const modal = page.getByRole("dialog", { name: "Task details", exact: true });
  await expect(modal).toBeVisible();
  await modal.getByRole("button", { name: "Resume", exact: true }).click();
  await expect(page.getByText("Resume task", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect
    .poll(() =>
      page
        .getByText("Resume task", { exact: true })
        .evaluate((element) => !!element.closest("[inert]")),
    )
    .toBe(true);
  await expect(modal).toBeVisible();
  await expect(page).toHaveURL(/\/tasks\/paused-task$/);
  await page.keyboard.press("Escape");
  await expect(page).toHaveURL(/\/tasks$/);
});

test("artifact alias keeps focus while typing and file tree supports keyboard navigation", async ({
  page,
}) => {
  await page.goto("/artifacts/artifact-one");
  const alias = page.getByRole("textbox", { name: "Artifact alias" });
  await alias.click();
  await alias.pressSequentially("production", { delay: 30 });
  await expect(alias).toHaveValue("production");
  await expect(alias).toBeFocused();
  const folder = page.getByRole("treeitem", { name: /weights/ });
  await folder.focus();
  await folder.press("ArrowRight");
  await expect(page.getByRole("treeitem", { name: /model.bin/ })).toBeVisible();
  await page.reload();
  await expect(
    page.getByRole("dialog", { name: "Artifact details" }),
  ).toBeVisible();
});

test("delayed resume validates the time and sends only the new schedule", async ({
  page,
}) => {
  await page.goto("/tasks/paused-task");
  await page.getByRole("button", { name: "Resume", exact: true }).click();
  await page.getByRole("switch", { name: "Resume later", exact: true }).click();
  const time = page.getByRole("textbox", { name: "Resume time", exact: true });
  await time.fill("2020-01-01T12:00");
  await page.getByRole("button", { name: "Schedule resume" }).click();
  await expect(
    page.getByText("Choose a resume time in the future.", { exact: true }),
  ).toBeVisible();
  const future = new Date(Date.now() + 86_400_000);
  const local = new Date(future.getTime() - future.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
  await time.fill(local);
  await time.focus();
  const refresh = page.waitForResponse((response) =>
    response.url().endsWith("/tasks/paused-task"),
  );
  await refresh;
  await expect(time).toBeFocused();
  const request = page.waitForRequest((request) =>
    request.url().endsWith("/paused-task/resume"),
  );
  await page.getByRole("button", { name: "Schedule resume" }).click();
  expect((await request).postDataJSON()).toEqual({
    scheduledAt: new Date(local).toISOString(),
  });
  await expect(
    page.getByRole("dialog", { name: "Task details" }),
  ).toBeVisible();
});

test("completed-task deletion resets the artifact option after dismissal", async ({
  page,
}) => {
  await page.route("**/api/v1/tasks/completed-task", (route) =>
    route.fulfill({
      json: {
        ...task,
        id: "completed-task",
        status: "completed",
        artifactId: "artifact-one",
      },
    }),
  );
  await page.goto("/tasks/completed-task");
  await page
    .getByRole("dialog", { name: "Task details" })
    .getByRole("button", { name: "Delete task", exact: true })
    .click();
  const option = page.getByRole("checkbox", {
    name: "Also delete the published artifact and model files",
  });
  await option.check();
  await page
    .getByRole("dialog")
    .filter({ has: option })
    .getByRole("button", { name: "Cancel", exact: true })
    .click();
  await page
    .getByRole("dialog", { name: "Task details" })
    .getByRole("button", { name: "Delete task", exact: true })
    .click();
  await expect(option).not.toBeChecked();
  await option.check();
  const request = page.waitForRequest(
    (request) => request.method() === "DELETE",
  );
  await page
    .getByRole("dialog")
    .filter({ has: option })
    .getByRole("button", { name: "Delete task", exact: true })
    .click();
  expect((await request).url()).toContain(
    "/completed-task?deleteArtifact=true",
  );
  await expect(page).toHaveURL(/\/tasks$/);
});

test("browser history closes a detail route and returns focus to its opener", async ({
  page,
}) => {
  await page.goto("/artifacts");
  const opener = page.getByRole("button", {
    name: "View details",
    exact: true,
  });
  await opener.click();
  await expect(page).toHaveURL(/\/artifacts\/artifact-one$/);
  await expect(
    page.getByRole("dialog", { name: "Artifact details" }),
  ).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/artifacts$/);
  await expect(opener).toBeFocused();
});

test("integration code uses beUI copy feedback and sanitizes Markdown HTML", async ({
  page,
}) => {
  await page.route("**/integration.md", (route) =>
    route.fulfill({
      contentType: "text/markdown",
      body: '# Integration\n\nIntro.\n\n## Setup\n\n```bash\necho hello\n```\n\n<img src=x onerror="window.injected=true">',
    }),
  );
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "clipboard", {
      value: {
        writeText: async () => {
          throw new Error("Clipboard denied");
        },
      },
    });
  });
  await page.goto("/integration#setup");
  const copy = page.getByRole("button", { name: "Copy code" });
  await expect(page.getByRole("heading", { name: "Setup" })).toBeVisible();
  await expect(copy).toBeVisible();
  await copy.click();
  await expect(
    page.getByText("Could not copy code", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Copied", exact: true }),
  ).toHaveCount(0);
  expect(await page.evaluate(() => "injected" in window)).toBe(false);
  expect(
    await page
      .getByRole("heading", { name: "Setup" })
      .evaluate((element) => element.getBoundingClientRect().top),
  ).toBeGreaterThanOrEqual(64);
});

test("theme preferences override the system, survive reloads, and recolor code", async ({
  page,
}, testInfo) => {
  await page.emulateMedia({ colorScheme: "light" });
  await page.goto("/tasks");
  const root = page.locator("html");
  const picker = page
    .getByRole("group", { name: "Theme", exact: true })
    .getByRole("button", { name: /^Theme:/ });
  async function expectMenuFullyVisible() {
    await expect
      .poll(async () => {
        const panel = await page
          .getByRole("listbox", { name: /^Theme:/ })
          .boundingBox();
        const lastOption = await page
          .getByRole("option", { name: "System", exact: true })
          .boundingBox();
        return (
          !!panel &&
          !!lastOption &&
          lastOption.y + lastOption.height <= panel.y + panel.height - 1
        );
      })
      .toBe(true);
  }
  await expect(root).toHaveAttribute("data-theme", "light");
  await expect(picker).toHaveText("Theme:System");
  const lightBackground = await page
    .locator("body")
    .evaluate((el) => getComputedStyle(el).backgroundColor);
  await picker.focus();
  await picker.press("Home");
  await expect(
    page.getByRole("option", { name: "Light", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(
    page.getByRole("option", { name: "Dark", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(root).toHaveAttribute("data-theme", "dark");
  await expect(picker).toBeFocused();
  expect(await root.evaluate((el) => getComputedStyle(el).colorScheme)).toBe(
    "dark",
  );
  await page.reload();
  await expect(root).toHaveAttribute("data-theme", "dark");
  await expect(picker).toHaveText("Theme:Dark");
  await page.route("**/integration.md", (route) =>
    route.fulfill({
      contentType: "text/markdown",
      body: "# Integration\n\n## Setup\n\n```ts\nconst answer = 42;\n```",
    }),
  );
  await page.goto("/integration");
  const token = page
    .locator('[data-code-block] span[style*="--agent-code-light"]')
    .first();
  await expect(token).toBeVisible();
  const darkColor = await token.evaluate((el) => getComputedStyle(el).color);
  await page.emulateMedia({ colorScheme: "dark" });
  await picker.click();
  await page.getByRole("option", { name: "Light", exact: true }).click();
  await expect(root).toHaveAttribute("data-theme", "light");
  expect(await root.evaluate((el) => getComputedStyle(el).colorScheme)).toBe(
    "light",
  );
  await expect
    .poll(() => token.evaluate((el) => getComputedStyle(el).color))
    .not.toBe(darkColor);
  await expect
    .poll(() =>
      page
        .locator("body")
        .evaluate((el) => getComputedStyle(el).backgroundColor),
    )
    .toBe(lightBackground);
  await picker.click();
  await page.getByRole("option", { name: "System", exact: true }).click();
  await expect(root).toHaveAttribute("data-theme", "dark");
  await page.emulateMedia({ colorScheme: "light" });
  await expect(root).toHaveAttribute("data-theme", "light");
  await page.emulateMedia({ colorScheme: "dark" });
  await expect(root).toHaveAttribute("data-theme", "dark");
  await page.setViewportSize({ width: 320, height: 844 });
  await picker.click();
  await expectMenuFullyVisible();
  await expect(
    page.getByRole("option", { name: "System", exact: true }),
  ).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("theme-menu-mobile.png") });
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1440, height: 844 });
  await picker.click();
  await expectMenuFullyVisible();
  await page.screenshot({
    path: testInfo.outputPath("theme-menu-desktop.png"),
  });
});

test("theme preferences synchronize between tabs", async ({
  page,
  context,
}) => {
  await page.goto("/tasks");
  const other = await context.newPage();
  const errors: string[] = [];
  other.on("pageerror", (error) => errors.push(error.message));
  try {
    await mockShelf(other);
    await other.goto("/artifacts");
    await page.getByRole("button", { name: /^Theme:/ }).click();
    await page.getByRole("option", { name: "Dark", exact: true }).click();
    await expect(other.getByRole("button", { name: /^Theme:/ })).toHaveText(
      "Theme:Dark",
    );
    await expect(other.locator("html")).toHaveAttribute("data-theme", "dark");
    await other.getByRole("button", { name: /^Theme:/ }).click();
    await other.getByRole("option", { name: "Light", exact: true }).click();
    await expect(page.getByRole("button", { name: /^Theme:/ })).toHaveText(
      "Theme:Light",
    );
    await expect(page.locator("html")).toHaveAttribute("data-theme", "light");
    expect(errors).toEqual([]);
  } finally {
    await other.close();
  }
});

test("login exposes theme selection even when browser storage is unavailable", async ({
  page,
}) => {
  await page.addInitScript(() =>
    Object.defineProperty(window, "localStorage", {
      get: () => {
        throw new Error("Storage disabled");
      },
    }),
  );
  await page.setViewportSize({ width: 320, height: 844 });
  await page.goto("/login");
  await page.getByRole("button", { name: /^Theme:/ }).click();
  await page.getByRole("option", { name: "Dark", exact: true }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.getByRole("textbox", { name: "Password" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});

test("mobile dark mode keeps primary pages and route-driven detail inside the viewport", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ colorScheme: "dark", reducedMotion: "reduce" });
  for (const path of [
    "/tasks",
    "/tasks/new",
    "/artifacts",
    "/artifacts/artifact-one",
  ]) {
    await page.goto(path);
    await expect(page.getByRole("navigation")).toBeVisible();
    if (path.endsWith("artifact-one"))
      await expect(
        page.getByRole("dialog", { name: "Artifact details" }),
      ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
      path,
    ).toBe(true);
    await page.screenshot({
      path: testInfo.outputPath(`${path.split("/").pop()}-mobile.png`),
    });
  }
});
