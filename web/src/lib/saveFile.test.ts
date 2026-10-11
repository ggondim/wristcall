import { afterEach, describe, expect, test, vi } from "vitest"
import { REVOKE_AFTER_MS, saveFile } from "./saveFile"

const blob = new Blob(["# calls"], { type: "text/markdown" })

function setNavigator(share: unknown, canShare: unknown): void {
  Object.defineProperty(navigator, "share", { value: share, configurable: true, writable: true })
  Object.defineProperty(navigator, "canShare", { value: canShare, configurable: true, writable: true })
}

afterEach(() => {
  setNavigator(undefined, undefined)
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe("saveFile", () => {
  test("sharesTheFileWhereTheBrowserCan", async () => {
    const share = vi.fn(async () => {})
    setNavigator(share, () => true)
    expect(await saveFile(blob, "wristcall-calls.md")).toBe("done")
    const data = (share.mock.calls[0] as unknown as [{ files: File[] }])[0]
    expect(data.files).toHaveLength(1)
    expect(data.files[0]!.name).toBe("wristcall-calls.md")
  })

  test("closingTheShareSheetIsNotAFailure", async () => {
    setNavigator(async () => { throw new DOMException("closed", "AbortError") }, () => true)
    const create = vi.spyOn(URL, "createObjectURL")
    expect(await saveFile(blob, "a.md")).toBe("done")
    expect(create).not.toHaveBeenCalled()
  })

  test("aRefusedShareWithoutATapAsksForOne", async () => {
    setNavigator(async () => { throw new DOMException("no gesture", "NotAllowedError") }, () => true)
    const create = vi.spyOn(URL, "createObjectURL")
    expect(await saveFile(blob, "a.md")).toBe("needs-tap")
    expect(create).not.toHaveBeenCalled()
  })

  test("aRefusedShareAfterATapFallsBackToDownload", async () => {
    setNavigator(async () => { throw new DOMException("no", "NotAllowedError") }, () => true)
    vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x")
    vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {})
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {})
    expect(await saveFile(blob, "a.md", true)).toBe("done")
  })

  test("downloadsWithAnAnchorAndRevokesTheUrlLater", async () => {
    vi.useFakeTimers()
    const create = vi.spyOn(URL, "createObjectURL").mockReturnValue("blob:x")
    const revoke = vi.spyOn(URL, "revokeObjectURL").mockImplementation(() => {})
    let seen: { href: string; download: string } | null = null
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      seen = { href: this.href, download: this.download }
    })
    expect(await saveFile(blob, "wristcall-calls.md")).toBe("done")
    expect(create).toHaveBeenCalledWith(blob)
    expect(seen).toEqual({ href: "blob:x", download: "wristcall-calls.md" })
    expect(document.querySelector("a[download]")).toBeNull()
    expect(revoke).not.toHaveBeenCalled()
    vi.advanceTimersByTime(REVOKE_AFTER_MS)
    expect(revoke).toHaveBeenCalledWith("blob:x")
  })
})
