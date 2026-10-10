import { describe, expect, test } from "vitest"
import { canonical, parseServerAddress, sameUrl } from "./address"

describe("address", () => {
  test("address: http only on loopback", () => {
    expect(parseServerAddress("http://192.168.0.2:8765")).toBeNull()
    expect(parseServerAddress("http://home.example")).toBeNull()
    expect(parseServerAddress(" https://Home.Example/ ")?.toString()).toBe("https://home.example/")
    expect(parseServerAddress("http://localhost:8765")).not.toBeNull()
    expect(parseServerAddress("http://127.0.0.1:8765")).not.toBeNull()
    expect(parseServerAddress("http://[::1]:8765")).toBeNull() // not a CSP host source, so the page could not reach it
    expect(parseServerAddress("https://u:p@home.example")).toBeNull()
    expect(parseServerAddress("https://home.example/?x=1")).toBeNull()
    expect(parseServerAddress("https://home.example/#top")).toBeNull()
  })

  test("address: rejects other schemes, empty text and no host", () => {
    for (const bad of ["", "   ", "home.example", "ftp://home.example", "javascript:alert(1)", "https://", "https:///x"]) {
      expect(parseServerAddress(bad), bad).toBeNull()
    }
  })

  test("address: drops trailing slashes and keeps a path", () => {
    expect(parseServerAddress("https://home.example///")?.toString()).toBe("https://home.example/")
    expect(parseServerAddress("https://home.example/wc/")?.pathname).toBe("/wc")
  })

  test("address: canonical form", () => {
    expect(canonical(new URL("https://Home.Example:443/"))).toBe("https://home.example")
    expect(canonical(new URL("http://localhost:80"))).toBe("http://localhost")
    expect(canonical(new URL("http://localhost:8765/"))).toBe("http://localhost:8765")
    expect(canonical(new URL("https://home.example/wc/"))).toBe("https://home.example/wc")
  })

  test("address: sameUrl compares the canonical form", () => {
    expect(sameUrl("https://Home.Example/", "https://home.example:443")).toBe(true)
    expect(sameUrl("https://a.example", "https://b.example")).toBe(false)
    expect(sameUrl("not a url", "https://b.example")).toBe(false)
  })
})
