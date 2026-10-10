// Checks the build output the Vitest run cannot see (run after `npm run build`): the page and the worker must work
// under the image's CSP, which has no 'unsafe-inline', and the worker must be a classic script.
import { existsSync, readFileSync } from "node:fs"
import { join } from "node:path"
import { fileURLToPath } from "node:url"

/** Problems found in a dist directory; empty when it is fine. */
export function checkDist(dir) {
  const problems = []
  const read = (name) => {
    const file = join(dir, name)
    if (!existsSync(file)) {
      problems.push(`${name} is missing`)
      return null
    }
    return readFileSync(file, "utf8")
  }

  const html = read("index.html")
  if (html !== null) {
    for (const tag of html.matchAll(/<script\b([^>]*)>/gi)) {
      if (!/\bsrc\s*=/i.test(tag[1])) problems.push("index.html has an inline <script>")
    }
    if (/<style\b/i.test(html)) problems.push("index.html has a <style> element")
    if (/\sstyle\s*=/i.test(html)) problems.push("index.html has a style= attribute")
    if (/\son[a-z]+\s*=/i.test(html)) problems.push("index.html has an inline event handler")
  }

  const worker = read("sw.js")
  if (worker !== null) {
    // Top level only matters, but the worker is one minified IIFE: any import or export statement is a mistake.
    if (/(^|[;{}\n])\s*import\s*[\w{*"'(]/.test(worker)) problems.push("sw.js has an import")
    if (/(^|[;{}\n])\s*export\s*[\w{*]/.test(worker)) problems.push("sw.js has an export")
    if (!/\bwristcall-shell-[0-9a-f]{12}\b/.test(worker)) problems.push("sw.js was not given a build version")
    if (!/assets\/[\w.-]+\.js/.test(worker)) problems.push("sw.js has no asset list")
  }

  read("manifest.webmanifest")
  read("config.json")
  return problems
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const problems = checkDist(process.argv[2] ?? "dist")
  for (const problem of problems) console.error(`check:dist: ${problem}`)
  if (problems.length > 0) process.exit(1)
  console.log("check:dist: ok")
}
