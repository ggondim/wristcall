import { act, fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import type { WebSessionPoll, WebSessionStart } from "../api/cloud"
import { ApiError, NetworkError } from "../lib/errors"
import { AccountModel } from "../models/account"
import { SignInModel } from "../models/signIn"
import { FakeSession } from "../test/fakeAccount"
import { QrCode } from "./QrCode"
import { SignInView } from "./SignInView"

const IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
const MAC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15"

async function settle(): Promise<void> {
  for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve))
}

function setup(opts: { userAgent?: string; touchPoints?: number; poll?: () => Promise<WebSessionPoll> } = {}) {
  const timers: (() => void)[] = []
  let n = 0
  const cloud = {
    createWebSession: async (): Promise<WebSessionStart> => {
      n += 1
      return { id: `ws_${n}`, approve_key: `wck_${n}`, poll_key: `wcp_${n}`, expires_at: Date.now() / 1000 + 300 }
    },
    pollWebSession: opts.poll ?? (async (): Promise<WebSessionPoll> => ({ status: "pending", expires_at: Date.now() / 1000 + 300 })),
  }
  const calls: string[] = []
  const assigned: string[] = []
  const account = new AccountModel(new FakeSession("signedOut", calls), null, null, { assign: (url) => void assigned.push(url) })
  const model = new SignInModel({
    cloud,
    account,
    setTimer: (fn) => {
      timers.push(fn)
      return () => {
        const i = timers.indexOf(fn)
        if (i >= 0) timers.splice(i, 1)
      }
    },
    visibility: () => "visible",
    userAgent: opts.userAgent ?? MAC,
    touchPoints: opts.touchPoints ?? 0,
  })
  const fire = async () => {
    const fn = timers.shift()
    if (fn === undefined) throw new Error("no timer pending")
    act(() => fn())
    await settle()
  }
  const view = render(<SignInView model={model} account={account} />)
  return { model, account, calls, assigned, timers, fire, view }
}

beforeEach(() => {
  history.replaceState(null, "", "/")
  sessionStorage.clear()
})
afterEach(() => {
  history.replaceState(null, "", "/")
  sessionStorage.clear()
})

describe("SignInView", () => {
  test("shows the fixed copy", async () => {
    const { view } = setup()
    expect(await screen.findByRole("img", { name: "Sign-in code" })).toBeTruthy()
    expect(screen.getByRole("heading", { level: 1, name: "wristcall" })).toBeTruthy()
    expect(screen.getByText("Your agents, on the web.")).toBeTruthy()
    expect(screen.getByText("Edit agents, read and export your call history, and approve devices.")).toBeTruthy()
    expect(screen.getByRole("heading", { name: "Sign in" })).toBeTruthy()
    expect(screen.getByRole("heading", { name: "Scan with your iPhone" })).toBeTruthy()
    expect(screen.getByText("Point the Camera at the code, or open wristcall › Settings › Use on the web.")).toBeTruthy()
    expect(screen.getByText("Waiting for your iPhone…")).toBeTruthy()
    expect(screen.getByText("or")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Sign in with email" })).toBeTruthy()
    expect(screen.getByText("For accounts that already sign in with email. An email with no wristcall account creates a new, empty account.")).toBeTruthy()
    expect(screen.getByRole("link", { name: "Use your own server without an account" }).getAttribute("href")).toBe("/settings/servers/add")
    expect(view.container.textContent).not.toContain("wristcall users tokens add")
    expect(screen.queryByRole("link", { name: "Open wristcall app" })).toBeNull() // not on a Mac
    expect(view.container.querySelectorAll("[style]")).toHaveLength(0)
  })

  test("Open wristcall app only on an iPhone or iPad", async () => {
    setup({ userAgent: IPHONE })
    const open = await screen.findByRole("link", { name: "Open wristcall app" })
    expect(open.getAttribute("href")).toBe("wristcall://approve#w=ws_1&k=wck_1")
  })

  test("an iPad that says Macintosh is told by its touch screen", async () => {
    setup({ userAgent: MAC, touchPoints: 5 })
    expect(await screen.findByRole("link", { name: "Open wristcall app" })).toBeTruthy()
  })

  test("Sign in with email leaves for the email sign-in", async () => {
    const w = setup()
    fireEvent.click(screen.getByRole("button", { name: "Sign in with email" }))
    await waitFor(() => expect(w.assigned).toHaveLength(1))
    expect(w.calls).toContain("beginSignIn")
  })

  test("an expired code offers a new one", async () => {
    const w = setup({ poll: async () => Promise.reject(new ApiError(404, "not_found", "Not found.")) })
    await settle()
    for (let i = 0; i < 4; i++) await w.fire()
    expect(screen.getByText("Code expired.")).toBeTruthy()
    expect(screen.queryByRole("img", { name: "Sign-in code" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Show a new code" }))
    expect(await screen.findByRole("img", { name: "Sign-in code" })).toBeTruthy()
  })

  test("the device limit says what to do", async () => {
    const w = setup({ poll: async () => Promise.reject(new ApiError(403, "limit", "Too many devices.")) })
    await settle()
    await w.fire()
    expect(screen.getByText("Too many devices on this account. Remove one in wristcall on your iPhone.")).toBeTruthy()
    expect(screen.getByRole("button", { name: "Show a new code" })).toBeTruthy()
  })

  test("a Cloud out of reach offers Try again", async () => {
    const w = setup({ poll: async () => Promise.reject(new NetworkError()) })
    await settle()
    await w.fire()
    expect(screen.getByText("Can't reach wristcall Cloud.")).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    expect(await screen.findByRole("img", { name: "Sign-in code" })).toBeTruthy()
  })

  test("leaving the screen stops the model", async () => {
    const w = setup()
    await settle()
    expect(w.timers).toHaveLength(1)
    act(() => void w.view.unmount())
    expect(w.timers).toHaveLength(0)
  })
})

describe("QrCode", () => {
  test("QrCode has no style attribute", () => {
    const { container } = render(<QrCode value="https://wristcall-app.trigram.com.br/approve#w=ws_0123456789abcdef&k=wck_x" label="Sign-in code" />)
    const svg = container.querySelector("svg")!
    expect(svg.getAttribute("role")).toBe("img")
    expect(svg.getAttribute("aria-label")).toBe("Sign-in code")
    expect(svg.getAttribute("viewBox")).toMatch(/^0 0 (\d+) \1$/)
    expect(container.querySelectorAll("path")).toHaveLength(1)
    expect(container.querySelectorAll("[style]")).toHaveLength(0)
    expect(container.innerHTML).not.toContain("style")
  })

  test("the quiet zone is four modules wide", () => {
    const { container } = render(<QrCode value="hello" label="Code" />)
    const size = Number(container.querySelector("svg")!.getAttribute("viewBox")!.split(" ")[2])
    const d = container.querySelector("path")!.getAttribute("d")!
    const starts = [...d.matchAll(/M(\d+) (\d+)/g)].map((m) => [Number(m[1]), Number(m[2])])
    expect(Math.min(...starts.map(([x]) => x!))).toBe(4)
    expect(Math.min(...starts.map(([, y]) => y!))).toBe(4)
    expect(size).toBe(21 + 8) // version 1 plus the border on both sides
  })
})
