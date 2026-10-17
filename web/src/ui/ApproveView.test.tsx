import { act, cleanup, fireEvent, render, screen } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { AccountModel } from "../models/account"
import { ServersModel } from "../models/servers"
import { SignInModel } from "../models/signIn"
import { FakeSession } from "../test/fakeAccount"
import { App } from "./App"
import { ApproveWatchSignInView } from "./ApproveWatchSignInView"
import { BaseModel } from "./useModel"

const HASH = "#w=ws_0123456789abcdef&k=wck_abc-_9"
const emptyStore = { list: async () => [], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null }

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

function app(opts: { signedIn?: boolean } = {}) {
  const account = new AccountModel(new FakeSession(opts.signedIn ? "signedIn" : "signedOut", []), null, null, { assign: () => {} })
  const cloud = {
    createWebSession: vi.fn(async () => ({ id: "ws_fedcba9876543210", approve_key: "wck_1", poll_key: "wcp_1", expires_at: Date.now() / 1000 + 300 })),
    pollWebSession: vi.fn(async () => ({ status: "pending" as const, expires_at: Date.now() / 1000 + 300 })),
  }
  const signIn = new SignInModel({ cloud, account, setTimer: () => () => {}, visibility: () => "visible" })
  render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} account={account} signIn={signIn} />)
  return { cloud }
}

beforeEach(() => history.replaceState(null, "", `/approve${HASH}`))
afterEach(() => {
  cleanup()
  history.replaceState(null, "", "/")
  sessionStorage.clear()
})

describe("ApproveView", () => {
  test("shows Open in wristcall with the wristcall:// link", async () => {
    app()
    expect(await screen.findByRole("heading", { name: "Approve on your iPhone" })).toBeTruthy()
    expect(screen.getByText("This code signs a browser in to your wristcall account. Open it in the wristcall app to approve.")).toBeTruthy()
    expect(screen.getByRole("link", { name: "Open in wristcall" }).getAttribute("href")).toBe("wristcall://approve#w=ws_0123456789abcdef&k=wck_abc-_9")
    expect(screen.getByText("Requires the wristcall iPhone app.")).toBeTruthy()
    expect(screen.getByText("Only approve a code you opened yourself.")).toBeTruthy()
  })

  test("stores nothing and keeps the code in the address bar", async () => {
    app()
    await screen.findByRole("link", { name: "Open in wristcall" })
    expect(location.hash).toBe(HASH)
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.length).toBe(0)
  })

  test.each(["", "#w=ws_0123456789abcdef", `#w=ws_0123456789abcdef&k=${"a".repeat(129)}`, "#w=ws_BAD&k=wck_a"])("bad link %j shows the error", async (hash) => {
    history.replaceState(null, "", `/approve${hash}`)
    app()
    expect((await screen.findByRole("alert")).textContent).toBe("This link is not valid. Scan the code again.")
    expect(screen.queryByRole("link", { name: "Open in wristcall" })).toBeNull()
  })

  test("/approve has no tabs", async () => {
    app({ signedIn: true })
    await screen.findByRole("heading", { name: "Approve on your iPhone" })
    expect(screen.queryByRole("navigation", { name: "Main" })).toBeNull()
  })

  test("the sign-in screen never covers /approve", async () => {
    const { cloud } = app() // signed out, no servers: the first screen would take any other route
    await screen.findByRole("heading", { name: "Approve on your iPhone" })
    expect(screen.queryByRole("heading", { name: "Scan with your iPhone" })).toBeNull()
    await act(async () => {})
    expect(cloud.createWebSession).not.toHaveBeenCalled()
  })
})

describe("ApproveWatchSignInView", () => {
  test("watch approval shows the new-account warning", async () => {
    const cloud = {
      deleteAccount: async () => {},
      account: async () => ({ account: "acc-1", created_at: "", servers: 0, plan: null, identities: ["zitadel"], key_set: true }),
      redeemInvite: async () => ({ plan: "full", source: "beta", expires_at: null }),
      devices: async () => [],
      removeDevice: async () => {},
    }
    const account = new AccountModel(new FakeSession("signedIn", []), null, null, { assign: () => {}, cloud })
    render(<ApproveWatchSignInView model={account} />)
    const field = await screen.findByLabelText("Code shown on the watch")
    const warning = screen.getByText(
      "Sign in with the email of your wristcall account. An email with no account creates a new, empty account, and the watch shows no agents.",
    )
    // Above the code field.
    expect(warning.compareDocumentPosition(field) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.input(field, { target: { value: "ABCD-EFGH" } })
  })
})
