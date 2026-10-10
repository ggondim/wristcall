import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"
import { describe, expect, test } from "vitest"
import { ICONS, iconShapes, iconSvg } from "./icons"

const swift = readFileSync(resolve(dirname(fileURLToPath(import.meta.url)), "../../../watch/Phone/IconPicker.swift"), "utf8")

describe("icons", () => {
  test("iconsMatchSwiftList", () => {
    const block = /static let symbols = \[([^\]]*)\]/.exec(swift)
    expect(block).not.toBeNull()
    const names = [...block![1]!.matchAll(/"([^"]+)"/g)].map((match) => match[1])
    expect(names).toHaveLength(20)
    expect([...ICONS]).toEqual(names)
  })

  test("everyIconIsOurOwnPlainDrawing", () => {
    for (const name of ICONS) {
      const svg = iconSvg(name)
      expect(svg, name).toContain('viewBox="0 0 24 24"')
      // Only paths and circles: no text, script, style, image, link or event handler.
      const tags = [...svg.matchAll(/<([a-zA-Z]+)/g)].map((match) => match[1])
      expect(tags.every((tag) => ["svg", "path", "circle"].includes(tag!)), name).toBe(true)
      expect(svg, name).not.toMatch(/\son[a-z]+=|href|<text|<script|<style/i)
      expect(svg, name).not.toBe(iconSvg("unknown.name"))
      for (const shape of iconShapes(name)) {
        if ("d" in shape) expect(shape.d, name).toMatch(/^[MmLlHhVvCcSsQqTtAaZz0-9eE.,\s-]+$/)
      }
    }
    expect(new Set(ICONS.map(iconSvg)).size).toBe(ICONS.length)
  })

  test("unknownIconIsGeneric", () => {
    const generic = iconSvg("not.an.icon")
    expect(generic).toContain("<circle")
    expect(iconSvg("")).toBe(generic)
    expect(iconSvg("person.wave.2")).toBe(generic)
    // Names that exist on every object are not icons.
    expect(iconSvg("constructor")).toBe(generic)
    expect(iconSvg("__proto__")).toBe(generic)
  })
})
