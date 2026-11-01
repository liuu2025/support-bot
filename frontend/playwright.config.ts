import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  testMatch: "workspace.spec.ts",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:7105",
    channel: "chrome",
    viewport: { width: 1440, height: 900 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  outputDir: "test-results",
  webServer: {
    command: "npm run dev -- --host 127.0.0.1 --port 7105 --strictPort",
    url: "http://127.0.0.1:7105",
    reuseExistingServer: !process.env.CI,
  },
});
