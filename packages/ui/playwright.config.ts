import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:5197",
    channel: process.env.PLAYWRIGHT_CHANNEL,
    reducedMotion: "reduce",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "pnpm dev --host 127.0.0.1 --port 5197 --strictPort",
    url: "http://127.0.0.1:5197",
    reuseExistingServer: false,
  },
});
