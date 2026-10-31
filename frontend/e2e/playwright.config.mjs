import { defineConfig } from "@playwright/test";

const baseURL = process.env.HA_URL?.replace(/\/$/, "");

export default defineConfig({
  testDir: ".",
  testMatch: "**/*.spec.mjs",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  retries: 0,
  workers: 1,
  reporter: [["list"], ["html", { outputFolder: "artifacts/report", open: "never" }]],
  use: {
    baseURL,
    storageState: process.env.HA_STORAGE_STATE || undefined,
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "retain-on-failure",
  },
  outputDir: "artifacts/results",
});
