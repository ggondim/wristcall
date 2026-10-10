import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, test } from "vitest"

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const read = (path: string) => readFileSync(resolve(root, path))

function pngSize(file: Buffer): string {
  expect(file.subarray(1, 4).toString()).toBe("PNG")
  return `${file.readUInt32BE(16)}x${file.readUInt32BE(20)}`
}

interface Icon {
  src: string
  sizes: string
  type: string
  purpose?: string
}

describe("manifest", () => {
  const manifest = JSON.parse(read("public/manifest.webmanifest").toString()) as Record<string, unknown> & { icons: Icon[] }

  test("manifest: installable standalone app on the site root", () => {
    expect(manifest).toMatchObject({ name: "wristcall", short_name: "wristcall", start_url: "/", scope: "/", display: "standalone" })
  })

  test("manifest: every icon exists with the size it declares, and one is maskable", () => {
    expect(manifest.icons.map((i) => i.sizes).sort()).toEqual(["192x192", "512x512", "512x512"])
    for (const icon of manifest.icons) expect(pngSize(read(`public${icon.src}`)), icon.src).toBe(icon.sizes)
    expect(manifest.icons.filter((i) => i.purpose === "maskable")).toHaveLength(1)
  })

  test("manifest: the home screen icon for iOS is 180 px", () => {
    expect(pngSize(read("public/icons/apple-touch-icon.png"))).toBe("180x180")
  })

  test("index.html links the manifest and the icon, with no inline script or style", () => {
    const html = read("index.html").toString()
    expect(html).toContain('<link rel="manifest" href="/manifest.webmanifest" />')
    expect(html).toContain('rel="apple-touch-icon"')
    expect(html).toContain("viewport-fit=cover")
    expect(html).toContain('name="theme-color"')
    expect(html).not.toMatch(/<style|\sstyle=/i)
    expect(html.match(/<script\b[^>]*>/g)!.every((t) => /\ssrc=/.test(t))).toBe(true)
  })

  test("config.json ships without an account", () => {
    expect(JSON.parse(read("public/config.json").toString())).toEqual({ cloudUrl: "" })
  })

  test("csp.json: the policy has no unsafe-inline and no wildcard origin", () => {
    const headers = JSON.parse(read("csp.json").toString()) as Record<string, string>
    const csp = headers["Content-Security-Policy"]!
    expect(csp).not.toContain("unsafe-inline")
    expect(csp).toContain("script-src 'self';")
    expect(csp).toContain("connect-src 'self' https: http://localhost:* http://127.0.0.1:*;")
    expect(headers["X-Content-Type-Options"]).toBe("nosniff")
  })
})
