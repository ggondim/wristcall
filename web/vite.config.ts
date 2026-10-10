import preact from "@preact/preset-vite"
import { readFileSync } from "node:fs"
import { defineConfig } from "vitest/config"

// One source for the response headers: `vite preview` (and so the e2e run) sends the same policy the image does.
const headers = JSON.parse(readFileSync(new URL("./csp.json", import.meta.url), "utf8")) as Record<string, string>

export default defineConfig({
  plugins: [preact()],
  build: {
    // Keeps the polyfill out of index.html (the CSP forbids inline script); the manifest lists the bundle for the service worker.
    modulePreload: { polyfill: false },
    manifest: true,
  },
  preview: { headers },
  test: {
    environment: "happy-dom",
    setupFiles: ["fake-indexeddb/auto", "./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.mjs"],
  },
})
