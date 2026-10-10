import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { afterEach, describe, expect, test } from "vitest"
import { checkDist } from "./check-dist.mjs"

const GOOD_HTML = '<!doctype html><html><head><link rel="stylesheet" href="/assets/a.css"></head><body><div id="app"></div><script type="module" crossorigin src="/assets/a.js"></script></body></html>'
const GOOD_SW = '(function(e){"use strict";var t="wristcall-shell-0123456789ab",n=["assets/a.js"];self.addEventListener("fetch",()=>{})})({});'
const dirs = []

function dist(files) {
  const dir = mkdtempSync(join(tmpdir(), "wc-dist-"))
  dirs.push(dir)
  const all = { "index.html": GOOD_HTML, "sw.js": GOOD_SW, "manifest.webmanifest": "{}", "config.json": "{}", ...files }
  for (const [name, text] of Object.entries(all)) {
    if (text === null) continue
    mkdirSync(join(dir, name, ".."), { recursive: true })
    writeFileSync(join(dir, name), text)
  }
  return dir
}

afterEach(() => dirs.splice(0).forEach((d) => rmSync(d, { recursive: true, force: true })))

describe("check-dist", () => {
  test("a clean build passes", () => {
    expect(checkDist(dist({}))).toEqual([])
  })

  test("buildHasNoInlineScript: inline script, style element and style attribute fail", () => {
    expect(checkDist(dist({ "index.html": "<script>alert(1)</script>" }))).toContain("index.html has an inline <script>")
    expect(checkDist(dist({ "index.html": "<style>a{}</style>" }))).toContain("index.html has a <style> element")
    expect(checkDist(dist({ "index.html": '<div style="color:red"></div>' }))).toContain("index.html has a style= attribute")
    expect(checkDist(dist({ "index.html": '<a onclick="x()"></a>' }))).toContain("index.html has an inline event handler")
  })

  test("swBuildHasNoImport: import and export fail, words inside strings do not", () => {
    expect(checkDist(dist({ "sw.js": 'import{t as e}from"./assets/x.js";' + GOOD_SW }))).toContain("sw.js has an import")
    expect(checkDist(dist({ "sw.js": GOOD_SW + "export{a};" }))).toContain("sw.js has an export")
    expect(checkDist(dist({ "sw.js": GOOD_SW + 'var s="please import it";' }))).toEqual([])
  })

  test("a worker without the version or the asset list fails", () => {
    expect(checkDist(dist({ "sw.js": "self.addEventListener('fetch',()=>{})" }))).toEqual([
      "sw.js was not given a build version",
      "sw.js has no asset list",
    ])
  })

  test("missing files fail", () => {
    expect(checkDist(dist({ "sw.js": null }))).toEqual(["sw.js is missing"])
  })
})
