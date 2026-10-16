import { afterEach, describe, expect, test } from "vitest"
import { appLinkURL, hasPendingLink, normalizeLinkCode, parseLinkFragment, savePendingLink, takePendingLink } from "./link"

const CODE = "K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
const HOME = "https://home.example.com"

afterEach(() => sessionStorage.clear())

describe("normalizeLinkCode", () => {
  test.each([CODE, CODE.toLowerCase(), "K7Q2-MZXW 4RQT-6PZJ 3H5N-2C7V BQ", ` ${CODE}\n`])("accepts %j", (raw) => {
    expect(normalizeLinkCode(raw)).toBe(CODE)
  })

  test.each([
    "",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VBQA",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB0",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB1",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB8",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB9",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VB=",
    "K7Q2MZXW4RQT6PZJ3H5N2C7V٣",
    "K7Q2MZXW4RQT6PZJ3H5N2C7V３",
    "K7Q2MZXW4RQT6PZJ3H5N2C7V_",
    "K7Q2MZXW4RQT6PZJ3H5N2C7V+Q",
    // letters that upper-case to ASCII ("ı" to "I", "ſ" to "S") are not ASCII
    "K7Q2MZXW4RQT6PZJ3H5N2C7VBı",
    "K7Q2MZXW4RQT6PZJ3H5N2C7VBſ",
  ])("refuses %j", (raw) => {
    expect(normalizeLinkCode(raw)).toBeNull()
  })
})

describe("parseLinkFragment", () => {
  test.each([
    `#v=1&s=${HOME}&c=${CODE}`,
    `v=1&s=${HOME}&c=${CODE}`,
    `#v=1&s=https%3A%2F%2Fhome.example.com&c=${CODE}`,
    `#c=${CODE}&s=${HOME}&v=1`,
    `#v=1&s=${HOME}/&c=${CODE}`,
    `#v=1&s=${HOME}&c=k7q2-mzxw4rqt6pzj3h5n2c7vbq`,
  ])("reads %j", (hash) => {
    const link = parseLinkFragment(hash)
    expect(link?.server.href).toBe("https://home.example.com/")
    expect(link?.code).toBe(CODE)
  })

  test("keeps the port and the path", () => {
    for (const hash of [`#v=1&s=https%3A%2F%2Fhome.example.com%3A8443%2Fwc&c=${CODE}`, `#v=1&s=https://home.example.com:8443/wc&c=${CODE}`]) {
      expect(parseLinkFragment(hash)?.server.href).toBe("https://home.example.com:8443/wc")
    }
  })

  test("a plus is a literal plus, never a space", () => {
    expect(parseLinkFragment(`#v=1&s=${HOME}/a+b&c=${CODE}`)?.server.pathname).toBe("/a+b")
  })

  test.each([
    // invalid percent escapes must not throw
    `#v=1&s=${HOME}&c=%`,
    `#v=1&s=%ZZ&c=${CODE}`,
    `#v=1&s=${HOME}%&c=${CODE}`,
    `#v=1&s=${HOME}&c=K7Q2MZXW4RQT6PZJ3H5N2C7VB%`,
    // well formed escapes that are not UTF-8
    `#v=1&s=${HOME}&c=%C3%28`,
    `#v=1&s=%FF&c=${CODE}`,
    `#v=1&s=${HOME}/%FF&c=${CODE}`,
    // space in s
    `#v=1&s=https://home example.com&c=${CODE}`,
    `#v=1&s=https://home%20example.com&c=${CODE}`,
    `#v=1&s=%20${HOME}&c=${CODE}`,
    // version
    `#v=2&s=${HOME}&c=${CODE}`,
    `#s=${HOME}&c=${CODE}`,
    // missing or bad code
    `#v=1&s=${HOME}`,
    `#v=1&s=${HOME}&c=`,
    `#v=1&s=${HOME}&c=SHORT`,
    `#v=1&s=${HOME}&c=K7Q2MZXW4RQT6PZJ3H5N2C7VB1`,
    `#v=1&s=${HOME}&c=K7Q2MZXW4RQT6PZJ3H5N2C7V+Q`,
    // s: query, fragment, http (even loopback), other schemes, empty, missing
    `#v=1&s=${HOME}?x=1&c=${CODE}`,
    `#v=1&s=https%3A%2F%2Fhome.example.com%3Fx%3D1&c=${CODE}`,
    `#v=1&s=https%3A%2F%2Fhome.example.com%23frag&c=${CODE}`,
    `#v=1&s=http://home.example.com&c=${CODE}`,
    `#v=1&s=http://localhost:8080&c=${CODE}`,
    `#v=1&s=http://127.0.0.1:8765&c=${CODE}`,
    `#v=1&s=ftp://home.example.com&c=${CODE}`,
    `#v=1&s=&c=${CODE}`,
    `#v=1&c=${CODE}`,
    // empty segments
    `#v=1&s=${HOME}&c=${CODE}&`,
    `#v=1&&s=${HOME}&c=${CODE}`,
    `#&v=1&s=${HOME}&c=${CODE}`,
    // duplicates and malformed pairs
    `#v=1&v=1&s=${HOME}&c=${CODE}`,
    `#v=1&s=${HOME}&s=https://other.example.com&c=${CODE}`,
    `#v=1&s=${HOME}&c=${CODE}&c=${CODE}`,
    `#v=1&s=${HOME}&c=${CODE}&novalue`,
    `#v=1&s=${HOME}&c=${CODE}&=x`,
    "#",
    "",
  ])("refuses %j", (hash) => {
    expect(parseLinkFragment(hash)).toBeNull()
  })
})

describe("appLinkURL", () => {
  test("rebuilds the fragment for the app from the checked values", () => {
    const link = parseLinkFragment(`#c=k7q2-mzxw4rqt6pzj3h5n2c7vbq&s=https%3A%2F%2Fhome.example.com%2F&v=1&x=ignored`)!
    expect(appLinkURL(link)).toBe(`wristcall://link#v=1&s=https://home.example.com&c=${CODE}`)
  })

  test("encodes the address like the server, keeping : and /", () => {
    const link = parseLinkFragment(`#v=1&s=https://home.example.com:8443/w+c&c=${CODE}`)!
    expect(appLinkURL(link)).toBe(`wristcall://link#v=1&s=https://home.example.com:8443/w%2Bc&c=${CODE}`)
    // what the app reads back is the same link
    const again = parseLinkFragment(appLinkURL(link).slice("wristcall://link".length))!
    expect(again.server.href).toBe(link.server.href)
    expect(again.code).toBe(CODE)
  })
})

describe("pending link", () => {
  const hash = `#v=1&s=${HOME}&c=${CODE}`

  test("is read once", () => {
    savePendingLink(hash, 1_000)
    expect(hasPendingLink(2_000)).toBe(true)
    expect(hasPendingLink(2_000)).toBe(true) // looking does not spend it
    expect(takePendingLink(2_000)).toBe(hash)
    expect(takePendingLink(2_000)).toBeNull()
    expect(hasPendingLink(2_000)).toBe(false)
  })

  test("expires after 900 seconds", () => {
    savePendingLink(hash, 1_000)
    expect(hasPendingLink(1_000 + 901_000)).toBe(false)
    expect(takePendingLink(1_000 + 901_000)).toBeNull()
    savePendingLink(hash, 1_000)
    expect(takePendingLink(1_000 + 900_000)).toBe(hash)
  })

  test("garbage in storage is no link", () => {
    sessionStorage.setItem("wristcall.pendingLink", "{nope")
    expect(hasPendingLink()).toBe(false)
    expect(takePendingLink()).toBeNull()
  })
})
