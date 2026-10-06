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
      .locator("table")
      .locator("..")
      .evaluate((element) => {
        element.scrollTop = 250;
        element.scrollLeft = 250;
      });
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
