import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { OidcError } from "../api/oidc"
import { ServerClient } from "../api/server"
import type { CloudAccount } from "../api/cloud"
import { ApiError, NetworkError } from "../lib/errors"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { FakeSession, ISSUER } from "../test/fakeAccount"
import { mockFetch } from "../test/mockFetch"
import { AccountModel, normalizeWatchCode, watchSignInUrl, type AgendaControl } from "./account"
import { savePendingLink, takePendingLink } from "./link"
import { ServersModel } from "./servers"


function saved(over: Partial<SavedServer> = {}): SavedServer {
  return {
    id: "1",
    name: "home",
    url: "https://home.example.com",
    token: "wc_pat_secret",
    linked: true,
    cloudServerId: "c-1",
    pushKey: null,
    pushEndpoint: null,
    addedAt: 1,
    ...over,
  }
}

async function world(
  opts: {
    state?: "signedOut" | "signedIn"
    session?: boolean
    deleteAccount?: () => Promise<void>
    push?: boolean
    agenda?: boolean
    /** The saved server answers (and `DELETE /v1/account/link` is recorded in `calls`); by default it is down. */
    reachable?: boolean
    unlinkStatus?: number
  } = {},
) {
  const calls: string[] = []
  const session = new FakeSession(opts.state ?? "signedIn", calls)
  const agenda: AgendaControl = {
    notice: null,
    pushAll: () => void calls.push("agenda.pushAll"),
    suspend: () => void calls.push("agenda.suspend"),
    resume: () => void calls.push("agenda.resume"),
    invalidate: () => void calls.push("agenda.invalidate"),
  }
  const push = { accountDeleted: vi.fn(async () => void calls.push("push.accountDeleted")) }
  const cloud = {
    deleteAccount: vi.fn(opts.deleteAccount ?? (async () => void calls.push("cloud.deleteAccount"))),
    account: vi.fn(async (): Promise<CloudAccount> => ({ account: "acc-1", created_at: "", servers: 0, plan: null, identities: ["zitadel"], key_set: true })),
    redeemInvite: vi.fn(async () => ({ plan: "full", source: "beta", expires_at: null })),
    devices: vi.fn(async () => []),
    removeDevice: vi.fn(async () => {}),
  }
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  await store.put(saved())
  const servers = new ServersModel(store, (server) => {
    if (opts.reachable !== true) throw new Error("offline")
    return new ServerClient(
      new URL(server.url),
      server.token,
      mockFetch((url, init) => {
        if (init.method === "DELETE" && url.endsWith("/v1/account/link")) {
          calls.push("server.unlink")
          return new Response(null, { status: opts.unlinkStatus ?? 204 })
        }
        return new Response("{}", { status: 200 })
      }),
    )
  })
  await servers.load()
  const assigned: string[] = []
  const model = new AccountModel(opts.session === false ? null : session, opts.agenda === false ? null : agenda, opts.push === false ? null : push, {
    cloud,
    servers,
    assign: (url) => void assigned.push(url),
    refreshAgenda: () => void calls.push("agenda.refresh"),
  })
  return { calls, session, agenda, push, cloud, store, servers, assigned, model }
}

const QR_SESSION = { account: "acc-qr", device: "dev-qr", access_token: "at", refresh_token: "rt", expires_at: 2_000_000_000 }

beforeEach(() => history.replaceState(null, "", "/auth/callback?code=the-code&state=s"))
afterEach(() => {
  history.replaceState(null, "", "/")
  vi.restoreAllMocks()
})

describe("AccountModel screens", () => {
  test("without a session the account is unavailable and does nothing", async () => {
    const w = await world({ session: false })
    expect(w.model.state.screen).toBe("unavailable")
    await w.model.signIn()
    await w.model.signOut()
    await w.model.deleteAccount()
    expect(w.assigned).toEqual([])
    expect(w.model.state.screen).toBe("unavailable")
  })

  test("starts signed in or out as the session is", async () => {
    expect((await world({ state: "signedIn" })).model.state.screen).toBe("signedIn")
    expect((await world({ state: "signedOut" })).model.state.screen).toBe("signedOut")
  })

  test("start: pushes the agenda when signed in, not otherwise", async () => {
    const signedIn = await world({ state: "signedIn" })
    signedIn.model.start()
    expect(signedIn.calls).toEqual(["agenda.pushAll"])
    const signedOut = await world({ state: "signedOut" })
    signedOut.model.start()
    expect(signedOut.calls).toEqual([])
  })
})

describe("sign in", () => {
  test("signIn: leaves the page for the authorization URL", async () => {
    const w = await world({ state: "signedOut" })
    await w.model.signIn()
    expect(w.assigned).toEqual([`${ISSUER}/oauth/v2/authorize?state=s`])
    expect(w.model.state.screen).toBe("signingIn")
  })

  test("signIn: a failure shows its text and stays on the page", async () => {
    const w = await world({ state: "signedOut" })
    w.session.beginError = new OidcError("invalid", "This Wristcall Cloud doesn't accept sign-in from the web app yet.")
    await w.model.signIn()
    expect(w.model.state.screen).toEqual({ failed: "This Wristcall Cloud doesn't accept sign-in from the web app yet." })
    expect(w.assigned).toEqual([])
    w.session.beginError = new NetworkError()
    await w.model.signIn()
    expect(w.model.state.screen).toEqual({ failed: "Can't reach Wristcall Cloud." })
    w.session.beginError = new Error("boom with wc_pat_secret")
    await w.model.signIn()
    expect(JSON.stringify(w.model.state)).not.toContain("wc_pat_secret")
  })

  test("completeSignInCleansUrl: the code leaves the address bar and the agenda is mirrored", async () => {
    const w = await world({ state: "signedOut" })
    const replace = vi.spyOn(history, "replaceState")
    const href = location.href
    await w.model.completeSignIn(href)
    expect(replace).toHaveBeenCalledWith(null, "", "/agents")
    expect(location.pathname + location.search).toBe("/agents")
    expect(w.model.state.screen).toBe("signedIn")
    expect(w.calls).toEqual([`completeSignIn:${href}`, "agenda.pushAll"])
  })

  test("completeSignInCleansUrl: the code is gone from the address while the exchange is still running", async () => {
    const w = await world({ state: "signedOut" })
    let release!: () => void
    w.session.completeGate = new Promise((resolve) => (release = resolve))
    expect(location.search).toContain("code=the-code")
    const href = location.href
    const done = w.model.completeSignIn(href)
    expect(location.search).toBe("")
    expect(location.pathname).toBe("/auth/callback")
    expect(w.calls).toEqual([`completeSignIn:${href}`]) // the exchange got the address it had read
    release()
    await done
    expect(location.pathname).toBe("/agents")
  })

  test("completeSignIn: a failure keeps the page, clears the code and offers a retry", async () => {
    const w = await world({ state: "signedOut" })
    w.session.completeError = new OidcError("state", "The sign-in answer does not belong to this sign-in.")
    await w.model.completeSignIn(location.href)
    expect(w.model.state.screen).toEqual({ failed: "The sign-in answer does not belong to this sign-in." })
    expect(location.pathname).toBe("/auth/callback")
    expect(location.search).toBe("")
    expect(w.calls).not.toContain("agenda.pushAll")
  })

  test("completeSignIn: a server link waiting for the sign-in opens again, still pending", async () => {
    const w = await world({ state: "signedOut" })
    const hash = "#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ"
    savePendingLink(hash)
    try {
      await w.model.completeSignIn(location.href)
      expect(location.pathname + location.search + location.hash).toBe("/link")
      expect(takePendingLink()).toBe(hash) // the link screen takes it
    } finally {
      sessionStorage.clear()
    }
  })

  test("completeSignIn: called twice runs once", async () => {
    const w = await world({ state: "signedOut" })
    await Promise.all([w.model.completeSignIn(location.href), w.model.completeSignIn(location.href)])
    expect(w.calls.filter((c) => c.startsWith("completeSignIn"))).toHaveLength(1)
  })

  test("adopt: a device session from the QR sign-in signs in, mirrors the agenda and opens Agents", async () => {
    const w = await world({ state: "signedOut" })
    history.replaceState(null, "", "/")
    await w.model.adopt(QR_SESSION)
    expect(w.calls).toEqual(["adoptWebSession:dev-qr", "agenda.pushAll"])
    expect(w.model.state).toMatchObject({ screen: "signedIn", error: null })
    expect(w.session).toMatchObject({ state: "signedIn", accountId: "acc-qr", deviceId: "dev-qr" })
    expect(location.pathname).toBe("/agents")
  })

  test("adopt: a server link waiting for the sign-in opens again", async () => {
    const w = await world({ state: "signedOut" })
    savePendingLink("#v=1&s=https://home.example.com&c=K7Q2MZXW4RQT6PZJ3H5N2C7VBQ")
    try {
      await w.model.adopt(QR_SESSION)
      expect(location.pathname).toBe("/link")
    } finally {
      sessionStorage.clear()
    }
  })

  test("adopt: a session that cannot be stored stays signed out", async () => {
    const w = await world({ state: "signedOut" })
    history.replaceState(null, "", "/")
    w.session.adoptError = new Error("Local storage failed.")
    await expect(w.model.adopt(QR_SESSION)).rejects.toThrow("Local storage failed.")
    expect(w.model.state.screen).toBe("signedOut")
    expect(w.calls).not.toContain("agenda.pushAll")
    expect(location.pathname).toBe("/")
  })
})

describe("sign out", () => {
  test("signOut stops the agenda, signs the session out and stays on the page (no end-session page)", async () => {
    const w = await world()
    await w.model.signOut()
    expect(w.calls).toEqual(["agenda.suspend", "session.signOut", "agenda.resume"])
    expect(w.model.state).toMatchObject({ screen: "signedOut", busy: false, error: null })
    expect(w.assigned).toEqual([])
  })

  test("signOut: busy while it waits (a renewal in another tab), and the session's own notice does not flip the screen early", async () => {
    const w = await world()
    let release!: () => void
    w.session.signOutGate = new Promise((resolve) => (release = resolve))
    const done = w.model.signOut()
    await Promise.resolve()
    expect(w.model.state).toMatchObject({ busy: true, screen: "signedIn" })
    await w.model.signOut() // a second tap does nothing
    expect(w.calls.filter((c) => c === "session.signOut")).toHaveLength(1)
    release()
    await done
    expect(w.model.state).toMatchObject({ busy: false, screen: "signedOut" })
  })

  test("signOut: ignored unless signed in", async () => {
    const w = await world({ state: "signedOut" })
    await w.model.signOut()
    expect(w.calls).toEqual([])
  })

  test("signedOutKeepsServers: the servers and their tokens stay, only the account links go", async () => {
    const w = await world()
    await w.model.signOut()
    expect(await w.store.get("1")).toMatchObject({ token: "wc_pat_secret", url: "https://home.example.com", linked: false, cloudServerId: null })
    expect(w.servers.state.rows).toHaveLength(1)
    expect(w.servers.state.fromAccount).toEqual([])
  })

  test("a session ended in another tab moves to signed out and stops the agenda", async () => {
    const w = await world()
    w.session.end(null)
    await vi.waitFor(() => expect(w.model.state.screen).toBe("signedOut"))
    expect(w.calls).toEqual(["agenda.suspend", "agenda.resume"])
    expect(await w.store.get("1")).toMatchObject({ token: "wc_pat_secret", linked: false })
    expect(w.assigned).toEqual([])
  })

  test("a device deleted by the Cloud signs out and says why", async () => {
    const w = await world()
    w.session.end("This browser was signed out. Sign in again.")
    await vi.waitFor(() => expect(w.model.state.screen).toEqual({ failed: "This browser was signed out. Sign in again." }))
    expect(w.calls).toEqual(["agenda.suspend", "agenda.resume"])
    expect((await w.store.get("1"))?.linked).toBe(false)
  })
})

describe("delete account", () => {
  test("deleteAccountFailureKeepsSession: the session stays, the agenda resumes and the reason shows", async () => {
    const w = await world({
      deleteAccount: async () => {
        throw new ApiError(503, "account_unavailable", "The account service is busy.")
      },
    })
    await w.model.deleteAccount()
    expect(w.model.state.screen).toBe("signedIn")
    expect(w.model.state.busy).toBe(false)
    expect(w.model.state.error).toMatch(/^Can't delete the account now\. /)
    expect(w.calls).toEqual(["agenda.suspend", "agenda.resume"])
    expect(w.push.accountDeleted).not.toHaveBeenCalled()
    expect(w.assigned).toEqual([])
    expect((await w.store.get("1"))?.linked).toBe(true)
  })

  test("deleteAccount: a network failure says so without the raw error", async () => {
    const w = await world({
      deleteAccount: async () => {
        throw new NetworkError("fetch failed to https://cloud.example.com with bearer abc")
      },
    })
    await w.model.deleteAccount()
    expect(w.model.state.error).toBe("Can't delete the account now. Can't reach Wristcall Cloud.")
  })

  test("deleteAccountDropsPushKeysAndSignsOut", async () => {
    const w = await world()
    await w.model.deleteAccount()
    expect(w.cloud.deleteAccount).toHaveBeenCalledWith("access-token")
    expect(w.calls).toEqual([
      "agenda.suspend",
      "cloud.deleteAccount",
      "push.accountDeleted",
      "agenda.suspend",
      "session.signOut",
      "agenda.resume",
    ])
    expect(w.model.state.screen).toBe("signedOut")
    expect(w.assigned).toEqual([])
  })

  test("deleteAccount: linked servers are unlinked first, then the account goes", async () => {
    const w = await world({ reachable: true })
    await w.model.deleteAccount()
    expect(w.calls.indexOf("server.unlink")).toBeGreaterThanOrEqual(0)
    expect(w.calls.indexOf("server.unlink")).toBeLessThan(w.calls.indexOf("cloud.deleteAccount"))
    expect(w.model.state.screen).toBe("signedOut")
  })

  test("deleteAccount: a server that was not linked, or is down, does not stop the deletion", async () => {
    const notLinked = await world({ reachable: true, unlinkStatus: 404 })
    await notLinked.model.deleteAccount()
    expect(notLinked.cloud.deleteAccount).toHaveBeenCalledTimes(1)
    const down = await world()
    await down.model.deleteAccount()
    expect(down.cloud.deleteAccount).toHaveBeenCalledTimes(1)
    expect(down.model.state.screen).toBe("signedOut")
  })

  test("deleteAccount: a failed deletion comes after the unlinking, keeps the session and says so", async () => {
    const w = await world({
      reachable: true,
      deleteAccount: async () => {
        throw new TypeError("offline")
      },
    })
    await w.model.deleteAccount()
    expect(w.calls).toContain("server.unlink")
    expect(w.model.state.screen).toBe("signedIn")
    expect(w.model.state.error).toMatch(/^Can't delete the account now\./)
  })

  test("deleteAccount: a push cleanup that fails does not keep the user signed in", async () => {
    const w = await world()
    w.push.accountDeleted.mockRejectedValueOnce(new Error("indexeddb"))
    await w.model.deleteAccount()
    expect(w.model.state.screen).toBe("signedOut")
  })

  test("deleteAccount: a session that already ended is a sign-out, not an error", async () => {
    const w = await world()
    w.session.tokenError = new OidcError("signed_out", "Your session ended. Sign in again.")
    await w.model.deleteAccount()
    expect(w.cloud.deleteAccount).not.toHaveBeenCalled()
    expect(w.model.state.screen).toBe("signedOut")
    expect(w.model.state.error).toBeNull()
    expect(w.calls.filter((c) => c === "agenda.resume")).toHaveLength(2) // the failed delete, then the ended session
    expect(w.calls.filter((c) => c === "agenda.suspend")).toHaveLength(2)
  })

  test("deleteAccount: ignored when signed out", async () => {
    const w = await world({ state: "signedOut" })
    await w.model.deleteAccount()
    expect(w.cloud.deleteAccount).not.toHaveBeenCalled()
  })
})

describe("watch sign-in", () => {
  test("watchSignInUrlNormalizes", () => {
    expect(watchSignInUrl(ISSUER, "abcd efgh")).toBe(`${ISSUER}/device?user_code=ABCD-EFGH`)
    expect(watchSignInUrl(ISSUER, "abcd-efgh")).toBe(`${ISSUER}/device?user_code=ABCD-EFGH`)
    expect(watchSignInUrl(ISSUER, "  ab12cd34 ")).toBe(`${ISSUER}/device?user_code=AB12-CD34`)
    expect(watchSignInUrl(`${ISSUER}/`, "ABCD-EFGH")).toBe(`${ISSUER}/device?user_code=ABCD-EFGH`)
    expect(watchSignInUrl("http://localhost:8080", "ABCD-EFGH")).toBe("http://localhost:8080/device?user_code=ABCD-EFGH")
  })

  test("watchSignInUrlRejects", () => {
    for (const bad of ["ABC", "", "ABCD-EFG!", "ABCD-EFGHI", "ABCDE-FGH", "ABCD--EFGH", "ABCD\tEFGH", "ABCD  EFGH", "ABCD\u00a0EFGH", "ABCD-EF-GH", "ÁBCD-EFGH", "ABCD\nEFGH x"]) {
      expect(watchSignInUrl(ISSUER, bad), bad).toBeNull()
    }
    expect(watchSignInUrl("http://evil.example", "ABCD-EFGH")).toBeNull()
    expect(watchSignInUrl("https://login.example.com?x=1", "ABCD-EFGH")).toBeNull()
    expect(watchSignInUrl("", "ABCD-EFGH")).toBeNull()
  })

  test("normalizeWatchCode: the code as the provider shows it", () => {
    expect(normalizeWatchCode("abcd efgh")).toBe("ABCD-EFGH")
    expect(normalizeWatchCode("abcdefg")).toBeNull()
  })

  test("approveWatchSignIn: opens the provider's device page in a new tab, without opener or referrer", async () => {
    const w = await world()
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    await w.model.loadIssuer()
    expect(w.model.state.issuerReady).toBe(true)
    w.model.approveWatchSignIn("abcd efgh")
    expect(open).toHaveBeenCalledWith(`${ISSUER}/device?user_code=ABCD-EFGH`, "_blank", "noopener,noreferrer")
    expect(w.model.state.watch).toBe("opened")
  })

  test("approveWatchSignIn: without the issuer a tap only starts loading and asks to tap again (never window.open after an await)", async () => {
    const w = await world()
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    expect(w.model.state.issuerReady).toBe(false)
    w.model.approveWatchSignIn("ABCD-EFGH")
    expect(open).not.toHaveBeenCalled()
    expect(w.model.state.watch).toEqual({ failed: "Reaching Wristcall Cloud. Tap again in a moment." })
    await vi.waitFor(() => expect(w.model.state.issuerReady).toBe(true))
    await w.model.loadIssuer()
    expect(open).not.toHaveBeenCalled()
    expect(w.model.state.watch).toBe("idle") // the request to tap again is gone
    expect(w.calls.filter((c) => c === "config")).toHaveLength(1)
    w.model.approveWatchSignIn("ABCD-EFGH")
    expect(open).toHaveBeenCalledTimes(1)
  })

  test("approveWatchSignIn: a bad code opens nothing and says what to enter", async () => {
    const w = await world()
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    w.model.approveWatchSignIn("ABC")
    expect(open).not.toHaveBeenCalled()
    expect(w.model.state.watch).toEqual({ failed: "Enter the 8-character code the watch shows." })
  })

  test("approveWatchSignIn: an issuer that is not safe opens nothing", async () => {
    const w = await world()
    w.session.issuer = "http://evil.example"
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    await w.model.loadIssuer()
    w.model.approveWatchSignIn("ABCD-EFGH")
    expect(open).not.toHaveBeenCalled()
    expect(w.model.state.watch).toEqual({ failed: "Wristcall Cloud isn't set up for this app." })
  })

  test("approveWatchSignIn: the Cloud not answering shows a message, and a retry works", async () => {
    const w = await world()
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    w.session.configError = new NetworkError()
    await w.model.loadIssuer()
    expect(w.model.state).toMatchObject({ issuerError: "Can't reach Wristcall Cloud.", issuerReady: false })
    w.session.configError = null
    await w.model.loadIssuer()
    expect(w.model.state).toMatchObject({ issuerError: null, issuerReady: true })
    w.model.approveWatchSignIn("ABCD-EFGH")
    expect(open).toHaveBeenCalledTimes(1)
    expect(w.model.state.watch).toBe("opened")
    w.model.resetWatchApproval()
    expect(w.model.state.watch).toBe("idle")
  })

  test("approveWatchSignIn: nothing when signed out", async () => {
    const w = await world({ state: "signedOut" })
    const open = vi.spyOn(window, "open").mockReturnValue(null)
    w.model.approveWatchSignIn("ABCD-EFGH")
    expect(open).not.toHaveBeenCalled()
  })
})

describe("plan and identities", () => {
  test("loadProfile reads the plan and the sign-in kinds once at a time", async () => {
    const w = await world()
    w.cloud.account.mockResolvedValue({ account: "acc-1", created_at: "", servers: 0, plan: { plan: "custom", source: "beta", expires_at: null }, identities: ["apple"], key_set: true })
    await Promise.all([w.model.loadProfile(), w.model.loadProfile()])
    expect(w.cloud.account).toHaveBeenCalledTimes(1)
    expect(w.cloud.account).toHaveBeenCalledWith("access-token")
    expect(w.model.state).toMatchObject({ plan: { plan: "custom" }, identities: ["apple"], profileError: null })
  })

  test("loadProfile: no plan stays null, a failure says so and a retry fixes it", async () => {
    const w = await world()
    w.cloud.account.mockRejectedValueOnce(new NetworkError())
    await w.model.loadProfile()
    expect(w.model.state).toMatchObject({ identities: null, profileError: "Can't reach Wristcall Cloud." })
    await w.model.loadProfile()
    expect(w.model.state).toMatchObject({ plan: null, identities: ["zitadel"], profileError: null })
  })

  test("loadProfile does nothing signed out", async () => {
    const w = await world({ state: "signedOut" })
    await w.model.loadProfile()
    expect(w.cloud.account).not.toHaveBeenCalled()
  })

  test("signing out forgets the profile, and an answer that arrives later is dropped", async () => {
    const w = await world()
    let answer!: (value: CloudAccount) => void
    w.cloud.account.mockReturnValueOnce(new Promise((resolve) => (answer = resolve)))
    const loading = w.model.loadProfile()
    await w.model.signOut()
    answer({ account: "acc-1", created_at: "", servers: 0, plan: null, identities: ["zitadel"], key_set: true })
    await loading
    expect(w.model.state.identities).toBeNull()
  })

  test("redeemInvite sends the trimmed code and shows the plan", async () => {
    const w = await world()
    expect(await w.model.redeemInvite("  CODE-1 ")).toBe(true)
    expect(w.cloud.redeemInvite).toHaveBeenCalledWith("access-token", "CODE-1")
    expect(w.model.state).toMatchObject({ plan: { plan: "full", source: "beta" }, redeem: "idle" })
  })

  test("redeemInvite reads the agenda again only when the invite is redeemed", async () => {
    const w = await world()
    w.cloud.redeemInvite.mockRejectedValueOnce(new ApiError(404, "x", "raw"))
    await w.model.redeemInvite("c")
    expect(w.calls).not.toContain("agenda.refresh")
    await w.model.redeemInvite("c")
    expect(w.calls.filter((call) => call === "agenda.refresh")).toHaveLength(1)
  })

  test("refreshAgenda reads only while signed in", async () => {
    const out = await world({ state: "signedOut" })
    out.model.refreshAgenda()
    expect(out.calls).not.toContain("agenda.refresh")
    const w = await world()
    w.model.refreshAgenda()
    expect(w.calls).toContain("agenda.refresh")
  })

  test("redeemInvite: an empty code is not sent", async () => {
    const w = await world()
    expect(await w.model.redeemInvite("   ")).toBe(false)
    expect(w.cloud.redeemInvite).not.toHaveBeenCalled()
  })

  test.each([
    [404, "This invite isn't valid."],
    [422, "This invite isn't valid."],
    [409, "This account already has a plan."],
    [429, "Wristcall Cloud refused the request."],
  ])("redeemInvite: %i", async (status, text) => {
    const w = await world()
    w.cloud.redeemInvite.mockRejectedValueOnce(new ApiError(status, "x", "raw"))
    expect(await w.model.redeemInvite("c")).toBe(false)
    expect(w.model.state.redeem).toEqual({ failed: text })
    expect(w.model.state.plan).toBeNull()
  })

  test("redeemInvite: a network failure says so", async () => {
    const w = await world()
    w.cloud.redeemInvite.mockRejectedValueOnce(new NetworkError())
    await w.model.redeemInvite("c")
    expect(w.model.state.redeem).toEqual({ failed: "Can't reach Wristcall Cloud." })
  })

  test("a 401 in Settings forces a renewal", async () => {
    const w = await world()
    w.cloud.account.mockRejectedValueOnce(new ApiError(401, "unauthorized", "raw"))
    await w.model.loadProfile()
    expect(w.calls).toContain("accessTokenRefused:access-token")
    w.cloud.redeemInvite.mockRejectedValueOnce(new ApiError(401, "unauthorized", "raw"))
    expect(await w.model.redeemInvite("CODE-1")).toBe(false)
    expect(w.calls.filter((c) => c.startsWith("accessTokenRefused"))).toHaveLength(2)
  })

  test("a 401 on the deletion forces a renewal, other failures do not", async () => {
    const w = await world({
      deleteAccount: async () => {
        throw new ApiError(401, "unauthorized", "raw")
      },
    })
    await w.model.deleteAccount()
    expect(w.calls).toContain("accessTokenRefused:access-token")
    expect(w.model.state.screen).toBe("signedIn")
    const other = await world()
    other.cloud.account.mockRejectedValueOnce(new ApiError(503, "busy", "raw"))
    await other.model.loadProfile()
    expect(other.calls.some((c) => c.startsWith("accessTokenRefused"))).toBe(false)
  })
})
