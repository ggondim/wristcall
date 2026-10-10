import { readFileSync } from "node:fs"
import { describe, expect, test } from "vitest"

// nginx/security-headers.conf is what the image sends; csp.json is what `vite preview` (so the e2e run) sends. They must
// be identical, so the e2e run sees the policy of the image. Both allow http loopback servers (the app accepts them).
const read = (name) => readFileSync(new URL(name, import.meta.url), "utf8")

/** `add_header Name "value" always;` lines as a name → value map. */
export function parseHeaders(conf) {
  const headers = {}
  for (const m of conf.matchAll(/^\s*add_header\s+(\S+)\s+"([^"]*)"\s+always;\s*$/gm)) headers[m[1]] = m[2]
  return headers
}

/** The brace-delimited blocks that open with `opener` (e.g. "location"), as their text including the opener. */
export function blocks(conf, opener) {
  const found = []
  const re = new RegExp(`(^|\\s)${opener}\\b[^{;]*\\{`, "g")
  for (let m = re.exec(conf); m; m = re.exec(conf)) {
    let depth = 1
    let i = m.index + m[0].length
    while (depth > 0 && i < conf.length) depth += conf[i] === "{" ? 1 : conf[i] === "}" ? -1 : 0, i++
    found.push(conf.slice(m.index, i).trim())
  }
  return found
}

describe("image headers", () => {
  const preview = JSON.parse(read("../csp.json"))
  const image = parseHeaders(read("../nginx/security-headers.conf"))

  test("cspMatchesPreview: same headers as csp.json", () => {
    expect(Object.keys(image).sort()).toEqual(Object.keys(preview).sort())
    for (const [name, value] of Object.entries(preview)) {
      expect(image[name], name).toBe(value)
    }
  })

  test("the policy allows http loopback servers only as host sources a CSP can express, and no unsafe-*", () => {
    const csp = image["Content-Security-Policy"]
    expect(csp).toContain("connect-src 'self' https: http://localhost:* http://127.0.0.1:*;")
    expect(csp).not.toContain("[")
    expect(csp).not.toContain("unsafe-")
  })

  test("everySecurityHeaderIsAlways: no security add_header without always (so 404s carry them)", () => {
    for (const file of ["../nginx/security-headers.conf", "../nginx/default.conf"]) {
      for (const line of read(file).split("\n").filter((l) => /^\s*add_header\b/.test(l))) {
        if (/add_header\s+Cache-Control\b/.test(line)) continue // the immutable one deliberately skips error responses
        expect(line, file).toMatch(/\balways;\s*$/)
      }
    }
  })

  test("locationsWithAddHeaderIncludeSecurityHeaders: nginx does not inherit add_header into a location that has its own", () => {
    const conf = read("../nginx/default.conf")
    const include = "include /etc/nginx/security-headers.conf;"
    expect(conf).toContain(include)
    const locations = blocks(conf, "location")
    expect(locations.length).toBeGreaterThan(0)
    for (const location of locations) {
      if (/\badd_header\b/.test(location)) expect(location).toContain(include)
    }
  })
})
