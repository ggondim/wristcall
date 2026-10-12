import { defineConfig } from "@playwright/test"

// End-to-end tests against a built site (`npm run build && npm run preview`, port 5173) and local wristcall server and
// Cloud. `docs/superpowers/execucao/e8/e2e.sh` of the private docs repo sets all of it up; the variables are read in
// `e2e/env.ts`. Specs that need one that is missing skip with a reason.
//
// One worker and a fixed order: servers, then push, then account (it ends by deleting the account, so it goes last).
// Each later project depends on the one before it, which is what fixes the order.
//
// No traces, screenshots or videos: a page of the sign-in flow must never end up in a report with a credential on it.
const baseURL = "http://localhost:5173"

export default defineConfig({
  testDir: "./e2e",
  workers: 1,
  fullyParallel: false,
  retries: 0,
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: process.env.WRISTCALL_E2E_JSON
    ? [["list"], ["json", { outputFile: process.env.WRISTCALL_E2E_JSON }]]
    : [["list"]],
  use: {
    baseURL,
    // The full Chromium in the new headless mode: the headless shell denies notifications.
    channel: "chromium",
    headless: true,
    locale: "en-US",
    trace: "off",
    screenshot: "off",
    video: "off",
  },
  projects: [
    { name: "servers", testMatch: /servers\.spec\.ts/ },
    { name: "push", testMatch: /push\.spec\.ts/, dependencies: ["servers"] },
    { name: "account", testMatch: /account\.spec\.ts/, dependencies: ["push"] },
  ],
})
