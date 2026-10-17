import { describe, expect, test } from "vitest"
import { browserDeviceName } from "./deviceName"

const UA = {
  chromeMac:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  safariIphone:
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
  edgeWindows:
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0",
  firefoxLinux: "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
  safariMac: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
  chromeAndroid: "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36",
  chromeIpad: "Mozilla/5.0 (iPad; CPU OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/141.0.0.0 Mobile/15E148 Safari/604.1",
  chromeOs: "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
  firefoxIphone: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/143.0 Mobile/15E148 Safari/605.1.15",
}

describe("browserDeviceName", () => {
  test.each([
    ["Chrome on macOS", UA.chromeMac],
    ["Safari on iPhone", UA.safariIphone],
    ["Edge on Windows", UA.edgeWindows],
    ["Firefox on Linux", UA.firefoxLinux],
    ["Safari on macOS", UA.safariMac],
    ["Chrome on Android", UA.chromeAndroid],
    ["Chrome on iPad", UA.chromeIpad],
    ["Chrome on ChromeOS", UA.chromeOs],
    ["Firefox on iPhone", UA.firefoxIphone],
  ])("%s", (expected, userAgent) => {
    expect(browserDeviceName(userAgent)).toBe(expected)
  })

  test("Web browser for an empty user agent", () => {
    expect(browserDeviceName("")).toBe("Web browser")
  })

  test("an unknown browser on a known system names the system", () => {
    expect(browserDeviceName("Mozilla/5.0 (Windows NT 10.0) SomeBrowser/1.0")).toBe("Web browser on Windows")
  })

  test("the result is at most 64 characters", () => {
    for (const userAgent of [...Object.values(UA), "", "x".repeat(500)]) expect(browserDeviceName(userAgent).length).toBeLessThanOrEqual(64)
  })
})
