import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { OidcError } from "../api/oidc"
import { AccountModel, type AgendaControl } from "../models/account"
import { ServersModel } from "../models/servers"
import { FakeSession, ISSUER } from "../test/fakeAccount"
import { DELETE_EXPLANATION } from "./AccountView"
import { App } from "./App"
import { BaseModel } from "./useModel"

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

const emptyStore = { list: async () => [], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null }

function setup(opts: { state?: "signedIn" | "signedOut"; agenda?: AgendaControl | null; deleteAccount?: () => Promise<void> } = {}) {
  const calls: string[] = []
  const session = new FakeSession(opts.state ?? "signedIn", calls)
  const assigned: string[] = []
  const deleteAccount = vi.fn(opts.deleteAccount ?? (async () => {}))
  const model = new AccountModel(session, opts.agenda ?? null, { accountDeleted: async () => {} }, {
    cloud: { deleteAccount },
    servers: { forgetAccount: async () => {} },
    assign: (url) => void assigned.push(url),
  })
  const servers = new ServersModel(emptyStore)
  const ui = () => render(<App badge={new Badge()} servers={servers} account={model} />)
  return { calls, session, assigned, deleteAccount, model, ui }
}

beforeEach(() => history.replaceState(null, "", "/settings"))
afterEach(() => {
  history.replaceState(null, "", "/")
  vi.restoreAllMocks()
})

describe("Settings", () => {
  test("noCloudHidesTheAccountSection", () => {
    render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} />)
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
    expect(screen.queryByText("Account")).toBeNull()
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull()
  })

  test("signedOutOffersSignIn", async () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.getByRole("heading", { name: "Account" })).toBeTruthy()
    expect(screen.getByText(/to add servers with a pairing code/)).toBeTruthy()
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    await waitFor(() => expect(w.assigned).toHaveLength(1))
    expect(screen.getByText("Signing in…")).toBeTruthy()
  })

  test("signInFailureShowsTheReasonAndKeepsTheButton", async () => {
    const w = setup({ state: "signedOut" })
    w.session.beginError = new OidcError("invalid", "This wristcall Cloud doesn't accept sign-in from the web app yet.")
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }))
    expect((await screen.findByRole("alert")).textContent).toBe("This wristcall Cloud doesn't accept sign-in from the web app yet.")
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy()
  })

  test("signedInShowsTheAccount", () => {
    const agenda: AgendaControl = { notice: "The server list could not be saved to your account.", pushAll() {}, suspend() {}, resume() {}, invalidate() {} }
    const w = setup({ agenda })
    w.ui()
    expect(screen.getByText("Signed in")).toBeTruthy()
    expect(screen.getByText("The server list could not be saved to your account.")).toBeTruthy()
    expect(screen.getByRole("link", { name: "Approve watch sign-in" }).getAttribute("href")).toBe("/settings/watch")
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy()
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull()
  })

  test("agendaNoticeFollowsTheAgenda", () => {
    const listeners = new Set<() => void>()
    const agenda: AgendaControl = {
      notice: null,
      pushAll() {},
      suspend() {},
      resume() {},
      invalidate() {},
      onNoticeChange(listener) {
        listeners.add(listener)
        return () => {
          listeners.delete(listener)
        }
      },
    }
    setup({ agenda }).ui()
    expect(screen.queryByText("Could not save.")).toBeNull()
    act(() => {
      ;(agenda as { notice: string | null }).notice = "Could not save."
      for (const listener of listeners) listener()
    })
    expect(screen.getByText("Could not save.")).toBeTruthy()
  })

  test("signOutShowsBusyWhileItWaitsThenSignedOut", async () => {
    const w = setup()
    let release!: () => void
    w.session.signOutGate = new Promise((resolve) => (release = resolve))
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }))
    const busy = await screen.findByRole("button", { name: "Signing out…" })
    expect((busy as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole("button", { name: "Delete account" }) as HTMLButtonElement).disabled).toBe(true)
    release()
    await screen.findByRole("button", { name: "Sign in" })
    expect(w.assigned).toHaveLength(1)
  })

  test("deleteAccountAsksFirstWithTheIPhoneText", async () => {
    const w = setup()
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }))
    const confirm = screen.getByRole("region", { name: "Delete account" })
    expect(confirm.textContent).toContain(
      "Deletes your wristcall account: your sign-in and your server list in wristcall Cloud. Your servers and their data stay as they are.",
    )
    expect(DELETE_EXPLANATION).toContain("Your servers and their data stay as they are.")
    expect(w.deleteAccount).not.toHaveBeenCalled()
    fireEvent.click(within(confirm).getByRole("button", { name: "Cancel" }))
    expect(screen.queryByRole("region", { name: "Delete account" })).toBeNull()
    expect(w.deleteAccount).not.toHaveBeenCalled()
  })

  test("deleteAccountConfirmedSignsOut", async () => {
    const w = setup()
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }))
    fireEvent.click(within(screen.getByRole("region", { name: "Delete account" })).getByRole("button", { name: "Delete account" }))
    await screen.findByRole("button", { name: "Sign in" })
    expect(w.deleteAccount).toHaveBeenCalledWith("access-token")
  })

  test("deleteAccountFailureShowsTheReasonAndStaysSignedIn", async () => {
    const w = setup({
      deleteAccount: async () => {
        throw new TypeError("offline")
      },
    })
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }))
    fireEvent.click(within(screen.getByRole("region", { name: "Delete account" })).getByRole("button", { name: "Delete account" }))
    const alert = await screen.findByRole("alert")
    expect(alert.textContent).toMatch(/^Can't delete the account now\./)
    expect(screen.getByText("Signed in")).toBeTruthy()
  })
})

describe("Approve watch sign-in", () => {
  beforeEach(() => history.replaceState(null, "", "/settings/watch"))

  test("openApprovalPageAfterTypingTheCode", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    const w = setup()
    w.ui()
    const button = screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement
    fireEvent.input(screen.getByLabelText("Code shown on the watch"), { target: { value: "abcd efgh" } })
    await waitFor(() => expect(button.disabled).toBe(false)) // enabled once the issuer is loaded
    fireEvent.click(button)
    expect(open).toHaveBeenCalledWith(`${ISSUER}/device?user_code=ABCD-EFGH`, "_blank", "noopener,noreferrer")
    expect(await screen.findByText("Check your watch.")).toBeTruthy()
  })

  test("theButtonWaitsForTheIssuerAndOffersARetryWhenItFails", async () => {
    const w = setup()
    w.session.configError = new TypeError("offline")
    w.ui()
    fireEvent.input(screen.getByLabelText("Code shown on the watch"), { target: { value: "ABCD-EFGH" } })
    expect((await screen.findByRole("alert")).textContent).toBe("Something went wrong. Try again.")
    expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(true)
    w.session.configError = null
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    await waitFor(() => expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull()
  })

  test("aBadCodeKeepsTheButtonOff", () => {
    const w = setup()
    w.ui()
    fireEvent.input(screen.getByLabelText("Code shown on the watch"), { target: { value: "ABC" } })
    expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(true)
  })

  test("signedOutAsksToSignInFirst", () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.getByText("Sign in to your wristcall account in Settings first.")).toBeTruthy()
    expect(screen.queryByLabelText("Code shown on the watch")).toBeNull()
  })

  test("withoutCloudTheRouteShowsSettings", () => {
    render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} />)
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
  })
})

describe("Callback", () => {
  beforeEach(() => history.replaceState(null, "", "/auth/callback?code=the-code&state=s"))

  test("callbackTradesTheCodeAndOpensSettings", async () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.queryByRole("navigation")).toBeNull()
    await screen.findByRole("heading", { name: "Account" })
    expect(location.pathname).toBe("/settings")
    expect(location.search).toBe("")
    expect(w.calls.filter((c) => c.startsWith("completeSignIn"))).toHaveLength(1)
    expect(screen.getByText("Signed in")).toBeTruthy()
  })

  test("callbackFailureShowsTheReasonAndTryAgain", async () => {
    const w = setup({ state: "signedOut" })
    w.session.completeError = new OidcError("state", "The sign-in answer does not belong to this sign-in.")
    w.ui()
    expect((await screen.findByRole("alert")).textContent).toBe("The sign-in answer does not belong to this sign-in.")
    expect(location.search).toBe("")
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    await waitFor(() => expect(w.assigned).toHaveLength(1))
  })

  test("callbackWhileSigningInShowsTheMessage", () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.getByText("Signing in…")).toBeTruthy()
  })

  test("callbackWhenAlreadySignedInJustOpensSettings", async () => {
    const w = setup()
    w.ui()
    await screen.findByRole("heading", { name: "Account" })
    expect(w.calls.filter((c) => c.startsWith("completeSignIn"))).toHaveLength(0)
  })

  test("callbackWithoutCloudSaysSo", () => {
    render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} />)
    expect(screen.getByText("Account sign-in is not available here.")).toBeTruthy()
  })
})
