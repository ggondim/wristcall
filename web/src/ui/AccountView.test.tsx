import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import type { CloudAccount, CloudDevice } from "../api/cloud"
import { OidcError } from "../api/oidc"
import { ApiError } from "../lib/errors"
import { AccountModel, type AccountCloud, type AgendaControl } from "../models/account"
import { ServersModel } from "../models/servers"
import { SignInModel } from "../models/signIn"
import { UsageModel } from "../models/usage"
import { FakeSession, ISSUER } from "../test/fakeAccount"
import { attachCloudRoute, cloudEntry, json, NODE, serversWith } from "../test/fakeServers"
import { DELETE_EXPLANATION } from "./AccountView"
import { App } from "./App"
import { BaseModel } from "./useModel"

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

const emptyStore = { list: async () => [], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null }

const PROFILE: CloudAccount = { account: "acc-1", created_at: "", servers: 0, plan: null, identities: ["zitadel"], key_set: true }
const DEVICES: CloudDevice[] = [
  { id: "dev-1", name: "Chrome on macOS", platform: "web", created_at: 1, last_seen_at: null, current: true },
  { id: "dev-2", name: "Gustavo's iPhone", platform: "ios", created_at: 1, last_seen_at: 2, current: false },
]

function setup(
  opts: { state?: "signedIn" | "signedOut"; agenda?: AgendaControl | null; deleteAccount?: () => Promise<void>; cloud?: Partial<AccountCloud>; unlink?: () => Promise<void> } = {},
) {
  const calls: string[] = []
  const session = new FakeSession(opts.state ?? "signedIn", calls)
  const assigned: string[] = []
  const deleteAccount = vi.fn(opts.deleteAccount ?? (async () => void calls.push("cloud.deleteAccount")))
  const cloud: AccountCloud = {
    deleteAccount,
    account: vi.fn(async () => PROFILE),
    redeemInvite: vi.fn(async () => ({ plan: "full", source: "beta", expires_at: null })),
    devices: vi.fn(async () => DEVICES),
    removeDevice: vi.fn(async () => {}),
    ...opts.cloud,
  }
  const unlinkAccounts = vi.fn(opts.unlink ?? (async () => void calls.push("servers.unlinkAccounts")))
  const model = new AccountModel(session, opts.agenda ?? null, { accountDeleted: async () => {} }, {
    cloud,
    servers: { forgetAccount: async () => {}, unlinkAccounts },
    assign: (url) => void assigned.push(url),
  })
  const servers = new ServersModel(emptyStore)
  const ui = () => render(<App badge={new Badge()} servers={servers} account={model} />)
  return { calls, session, assigned, cloud, deleteAccount, unlinkAccounts, model, ui }
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
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull()
  })

  test("Sign in row opens /sign-in", async () => {
    const w = setup({ state: "signedOut" })
    // A saved server keeps the first screen away, so Settings shows (C2).
    const saved = { id: "1", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1 }
    const servers = new ServersModel({ ...emptyStore, list: async () => [saved] }, () => {
      throw new Error("offline")
    })
    const signIn = new SignInModel({
      cloud: { createWebSession: () => new Promise(() => {}), pollWebSession: () => new Promise(() => {}) },
      account: w.model,
      visibility: () => "hidden",
    })
    render(<App badge={new Badge()} servers={servers} account={w.model} signIn={signIn} />)
    expect(await screen.findByRole("heading", { name: "Account" })).toBeTruthy()
    expect(screen.getByText(/to add servers with a pairing code/)).toBeTruthy()
    const row = await screen.findByRole("link", { name: "Sign in" })
    expect(row.getAttribute("href")).toBe("/sign-in")
    fireEvent.click(row)
    await waitFor(() => expect(location.pathname).toBe("/sign-in"))
    expect(w.assigned).toEqual([]) // the row does not start the email sign-in itself
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull()
  })

  test("a failed sign-in shows the reason and keeps the Sign in row", async () => {
    const w = setup({ state: "signedOut" })
    w.session.completeError = new OidcError("state", "The sign-in answer does not belong to this sign-in.")
    await w.model.completeSignIn("https://app.example/auth/callback?code=c&state=s")
    history.replaceState(null, "", "/settings")
    w.ui()
    expect((await screen.findByRole("alert")).textContent).toBe("The sign-in answer does not belong to this sign-in.")
    expect(screen.getByRole("link", { name: "Sign in" }).getAttribute("href")).toBe("/sign-in")
  })

  test("signedInShowsTheAccount", async () => {
    const agenda: AgendaControl = { notice: "The server list could not be saved to your account.", pushAll() {}, suspend() {}, resume() {}, invalidate() {} }
    const w = setup({ agenda })
    w.ui()
    expect(screen.getByText("Signed in")).toBeTruthy()
    expect(screen.getByText("The server list could not be saved to your account.")).toBeTruthy()
    expect((await screen.findByRole("link", { name: "Approve watch sign-in" })).getAttribute("href")).toBe("/settings/watch")
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy()
    expect(screen.queryByRole("link", { name: "Sign in" })).toBeNull()
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
    await screen.findByRole("link", { name: "Sign in" })
    expect(w.assigned).toEqual([]) // no end-session page
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
    await screen.findByRole("link", { name: "Sign in" })
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

describe("Plan, devices, sign out and delete", () => {
  const plan = (name: string | null, source: string | null = "beta") => ({ ...PROFILE, plan: name === null ? null : { plan: name, source, expires_at: null } })

  test("plan shows no plan", async () => {
    setup().ui()
    expect(await screen.findByText("No plan")).toBeTruthy()
    expect(screen.queryByText(/Beta ·/)).toBeNull()
  })

  test("plan shows Wristcall Cloud beta", async () => {
    setup({ cloud: { account: vi.fn(async () => plan("full")) } }).ui()
    expect(await screen.findByText("Wristcall Cloud")).toBeTruthy()
    expect(screen.getByText("Beta · invite")).toBeTruthy()
  })

  test("plan shows Cloud with your models", async () => {
    setup({ cloud: { account: vi.fn(async () => plan("custom", "invite")) } }).ui()
    expect(await screen.findByText("Cloud with your models")).toBeTruthy()
    expect(screen.getByText("Beta · invite")).toBeTruthy()
  })

  test("redeem shows plan", async () => {
    const w = setup()
    w.ui()
    await screen.findByText("No plan")
    fireEvent.input(screen.getByLabelText("Redeem invite"), { target: { value: " abc-123 " } })
    fireEvent.click(screen.getByRole("button", { name: "Redeem" }))
    expect(await screen.findByText("Wristcall Cloud")).toBeTruthy()
    expect(screen.getByText("Beta · invite")).toBeTruthy()
    expect(w.cloud.redeemInvite).toHaveBeenCalledWith("access-token", "abc-123")
    // A plan now: the invite field goes away.
    expect(screen.queryByLabelText("Redeem invite")).toBeNull()
  })

  test.each([
    [404, "invalid_code", "This invite isn't valid."],
    [422, "invalid", "This invite isn't valid."],
    [409, "already_entitled", "This account already has a plan."],
  ])("redeem %i says the reason", async (status, code, text) => {
    const w = setup({
      cloud: {
        redeemInvite: vi.fn(async () => {
          throw new ApiError(status, code, "raw server text")
        }),
      },
    })
    w.ui()
    await screen.findByText("No plan")
    fireEvent.input(screen.getByLabelText("Redeem invite"), { target: { value: "nope" } })
    fireEvent.click(screen.getByRole("button", { name: "Redeem" }))
    expect((await screen.findByRole("alert")).textContent).toBe(text)
    expect(screen.getByText("No plan")).toBeTruthy()
  })

  test("redeem 409 says already plan", async () => {
    const w = setup({
      cloud: {
        redeemInvite: vi.fn(async () => {
          throw new ApiError(409, "already_entitled", "x")
        }),
      },
    })
    w.ui()
    await screen.findByText("No plan")
    fireEvent.input(screen.getByLabelText("Redeem invite"), { target: { value: "code" } })
    fireEvent.click(screen.getByRole("button", { name: "Redeem" }))
    expect((await screen.findByRole("alert")).textContent).toBe("This account already has a plan.")
  })

  test("the plan that could not be read offers a retry", async () => {
    const account = vi.fn<AccountCloud["account"]>().mockRejectedValueOnce(new TypeError("offline")).mockResolvedValue(plan("full"))
    setup({ cloud: { account } }).ui()
    expect((await screen.findByRole("alert")).textContent).toBe("Something went wrong. Try again.")
    expect(screen.queryByRole("link", { name: "Approve watch sign-in" })).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    expect(await screen.findByText("Wristcall Cloud")).toBeTruthy()
  })

  test("devices list marks browser", async () => {
    setup().ui()
    const list = await screen.findByRole("list", { name: "Devices" })
    const items = within(list).getAllByRole("listitem")
    expect(items).toHaveLength(2)
    expect(items[0]!.textContent).toContain("Chrome on macOS")
    expect(items[0]!.textContent).toContain("Web")
    expect(within(items[0]!).getByText("This browser")).toBeTruthy()
    expect(items[1]!.textContent).toContain("Gustavo's iPhone")
    expect(items[1]!.textContent).toContain("iPhone")
    expect(within(items[1]!).queryByText("This browser")).toBeNull()
  })

  test("cannot remove browser list", async () => {
    setup().ui()
    const items = within(await screen.findByRole("list", { name: "Devices" })).getAllByRole("listitem")
    expect(within(items[0]!).queryByRole("button", { name: "Remove" })).toBeNull()
    expect(within(items[1]!).getByRole("button", { name: "Remove" })).toBeTruthy()
  })

  test("remove another device calls DELETE", async () => {
    const w = setup()
    w.ui()
    const items = within(await screen.findByRole("list", { name: "Devices" })).getAllByRole("listitem")
    fireEvent.click(within(items[1]!).getByRole("button", { name: "Remove" }))
    await waitFor(() => expect(within(screen.getByRole("list", { name: "Devices" })).getAllByRole("listitem")).toHaveLength(1))
    expect(w.cloud.removeDevice).toHaveBeenCalledTimes(1)
    expect(w.cloud.removeDevice).toHaveBeenCalledWith("access-token", "dev-2")
    expect(screen.getByText("This browser")).toBeTruthy()
  })

  test("a device that cannot be removed stays and says so", async () => {
    const w = setup({
      cloud: {
        removeDevice: vi.fn(async () => {
          throw new TypeError("offline")
        }),
      },
    })
    w.ui()
    const items = within(await screen.findByRole("list", { name: "Devices" })).getAllByRole("listitem")
    fireEvent.click(within(items[1]!).getByRole("button", { name: "Remove" }))
    expect((await screen.findByRole("alert")).textContent).toBe("Something went wrong. Try again.")
    expect(within(screen.getByRole("list", { name: "Devices" })).getAllByRole("listitem")).toHaveLength(2)
  })

  test("sign out deletes this device", async () => {
    const w = setup()
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }))
    await screen.findByRole("link", { name: "Sign in" })
    expect(w.calls).toContain("session.signOut") // the session deletes this browser's device at the Cloud
    expect(w.deleteAccount).not.toHaveBeenCalled()
    expect(w.cloud.removeDevice).not.toHaveBeenCalled()
    expect(screen.queryByRole("list", { name: "Devices" })).toBeNull()
  })

  test("delete account unlinks linked servers first", async () => {
    const w = setup()
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }))
    fireEvent.click(within(screen.getByRole("region", { name: "Delete account" })).getByRole("button", { name: "Delete account" }))
    await screen.findByRole("link", { name: "Sign in" })
    expect(w.calls.indexOf("servers.unlinkAccounts")).toBeGreaterThanOrEqual(0)
    expect(w.calls.indexOf("servers.unlinkAccounts")).toBeLessThan(w.calls.indexOf("cloud.deleteAccount"))
  })

  test("delete account goes on when an unlink fails", async () => {
    const w = setup({
      unlink: async () => {
        throw new TypeError("offline")
      },
    })
    w.ui()
    fireEvent.click(screen.getByRole("button", { name: "Delete account" }))
    fireEvent.click(within(screen.getByRole("region", { name: "Delete account" })).getByRole("button", { name: "Delete account" }))
    await screen.findByRole("link", { name: "Sign in" })
    expect(w.deleteAccount).toHaveBeenCalledTimes(1)
  })
})

describe("Approve watch sign-in", () => {
  beforeEach(() => history.replaceState(null, "", "/settings/watch"))

  test("openApprovalPageAfterTypingTheCode", async () => {
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    const w = setup()
    w.ui()
    const button = (await screen.findByRole("button", { name: "Open approval page" })) as HTMLButtonElement
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
    fireEvent.input(await screen.findByLabelText("Code shown on the watch"), { target: { value: "ABCD-EFGH" } })
    expect((await screen.findByRole("alert")).textContent).toBe("Something went wrong. Try again.")
    expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(true)
    w.session.configError = null
    fireEvent.click(screen.getByRole("button", { name: "Try again" }))
    await waitFor(() => expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(false))
    expect(screen.queryByRole("button", { name: "Try again" })).toBeNull()
  })

  test("aBadCodeKeepsTheButtonOff", async () => {
    const w = setup()
    w.ui()
    fireEvent.input(await screen.findByLabelText("Code shown on the watch"), { target: { value: "ABC" } })
    expect((screen.getByRole("button", { name: "Open approval page" }) as HTMLButtonElement).disabled).toBe(true)
  })

  test("signedOutAsksToSignInFirst", () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.getByText("Sign in to your wristcall account in Settings first.")).toBeTruthy()
    expect(screen.queryByLabelText("Code shown on the watch")).toBeNull()
  })

  test("watch approval needs an email account", async () => {
    const w = setup({ cloud: { account: vi.fn(async () => ({ ...PROFILE, identities: ["apple"] })) } })
    w.ui()
    expect(await screen.findByText("Approve the watch from wristcall on your iPhone.")).toBeTruthy()
    expect(screen.queryByLabelText("Code shown on the watch")).toBeNull()
    expect(screen.queryByRole("button", { name: "Open approval page" })).toBeNull()
  })

  test("the Approve watch sign-in row needs an email account", async () => {
    history.replaceState(null, "", "/settings")
    setup({ cloud: { account: vi.fn(async () => ({ ...PROFILE, identities: ["apple"] })) } }).ui()
    await screen.findByText("No plan")
    expect(screen.queryByRole("link", { name: "Approve watch sign-in" })).toBeNull()
  })

  test("withoutCloudTheRouteShowsSettings", () => {
    render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} />)
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy()
  })
})

describe("Callback", () => {
  beforeEach(() => history.replaceState(null, "", "/auth/callback?code=the-code&state=s"))

  test("callbackTradesTheCodeAndOpensAgents", async () => {
    const w = setup({ state: "signedOut" })
    w.ui()
    expect(screen.queryByRole("navigation")).toBeNull()
    await waitFor(() => expect(location.pathname).toBe("/agents"))
    expect(location.search).toBe("")
    expect(w.calls.filter((c) => c.startsWith("completeSignIn"))).toHaveLength(1)
    expect(w.model.state.screen).toBe("signedIn")
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

  test("callbackWhenAlreadySignedInJustOpensAgents", async () => {
    const w = setup()
    w.ui()
    await waitFor(() => expect(location.pathname).toBe("/agents"))
    expect(location.search).toBe("")
    expect(w.calls.filter((c) => c.startsWith("completeSignIn"))).toHaveLength(0)
  })

  test("callbackWithoutCloudSaysSo", () => {
    render(<App badge={new Badge()} servers={new ServersModel(emptyStore)} />)
    expect(screen.getByText("Account sign-in is not available here.")).toBeTruthy()
  })
})

describe("Plan usage", () => {
  const full: CloudAccount = { ...PROFILE, plan: { plan: "full", source: "beta", expires_at: null } }
  const me = { plan: full.plan, usage: { month: "2026-10", used_s: 779, quota_s: 36_000, remaining_s: 35_221 } }

  function settings(profile: CloudAccount, entry: boolean, fail: unknown = null) {
    const { servers, fetchMock } = serversWith([], { [NODE]: (path) => (path === "me" ? json(me) : null) })
    attachCloudRoute(servers, fetchMock).fail = fail
    if (entry) servers.setCloud(cloudEntry())
    const cloud: AccountCloud = {
      deleteAccount: async () => {},
      account: vi.fn(async () => profile),
      redeemInvite: vi.fn(async () => ({ plan: "full", source: "beta", expires_at: null })),
      devices: vi.fn(async () => DEVICES),
      removeDevice: vi.fn(async () => {}),
    }
    const model = new AccountModel(new FakeSession("signedIn", []), null, null, { cloud, usage: new UsageModel(servers) })
    render(<App badge={new Badge()} servers={servers} account={model} />)
    return { fetchMock, model }
  }

  test("plan row shows usage or unavailable", async () => {
    settings(full, true)
    expect(await screen.findByText("12 of 600 min used this month")).toBeTruthy()
    expect(screen.getByText("Beta · invite")).toBeTruthy()
    // A plan already: no invite to redeem.
    expect(screen.queryByLabelText("Redeem invite")).toBeNull()
    cleanup()

    // The node does not answer: the plan row says so, and nothing else changes.
    settings(full, true, new ApiError(503, "no_node_available", "No node can take this account right now.", 30))
    expect(await screen.findByText("Usage unavailable right now.")).toBeTruthy()
    expect(screen.getByText("Wristcall Cloud")).toBeTruthy()
    expect(screen.queryByRole("alert")).toBeNull()
    cleanup()

    // No plan: no usage line and no request for it. With the Cloud entry still listed (a lapsed plan), no invite field
    // either; without plan or entry, the invite field.
    const lapsed = settings(PROFILE, true)
    expect(await screen.findByText("No plan")).toBeTruthy()
    expect(screen.queryByLabelText("Redeem invite")).toBeNull()
    expect(screen.queryByText(/min used this month|Usage unavailable/)).toBeNull()
    expect(lapsed.fetchMock.calls).toEqual([])
    cleanup()
    settings(PROFILE, false)
    expect(await screen.findByLabelText("Redeem invite")).toBeTruthy()
  })
})
