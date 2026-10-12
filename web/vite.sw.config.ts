import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import { defineConfig } from "vite"

// Second build, run after the app's: the service worker is a classic script (no import), and it needs the hashed
// names of the bundle, which only exist once the app is built.
interface ManifestEntry {
  file: string
  css?: string[]
  assets?: string[]
}

function shellAssets(): { assets: string[]; version: string } {
  const manifest = JSON.parse(readFileSync("dist/.vite/manifest.json", "utf8")) as Record<string, ManifestEntry>
  const files = new Set<string>()
  for (const entry of Object.values(manifest)) {
    for (const file of [entry.file, ...(entry.css ?? []), ...(entry.assets ?? [])]) {
      if (file.startsWith("assets/")) files.add(file)
    }
  }
  const assets = [...files].sort()
  const hash = createHash("sha256").update(readFileSync("dist/index.html")).update(assets.join("\n")).digest("hex")
  return { assets, version: hash.slice(0, 12) }
}

const { assets, version } = shellAssets()

export default defineConfig({
  publicDir: false,
  define: {
    __WC_ASSETS: JSON.stringify(assets),
    __WC_CACHE: JSON.stringify(`wristcall-shell-${version}`),
  },
  build: {
    outDir: "dist",
    emptyOutDir: false,
    lib: { entry: "src/sw.ts", formats: ["iife"], name: "wcsw", fileName: () => "sw.js" },
  },
})
