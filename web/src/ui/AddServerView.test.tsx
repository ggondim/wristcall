import { act, fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { ServerClient } from "../api/server"
import { AddServerModel } from "../models/addServer"
import { ServersModel, type AccountLink } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { App } from "./App"
import { AddServerView } from "./AddServerView"
import { BaseModel } from "./useModel"

const CLOUD = "https://cloud.example.com"
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
const health = { status: "ok", version: "0.7.0", protocol: 1, account: { issuer: CLOUD, device_credential: "x" }, push: null }

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

function saved(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "1", name: "home", url: "https://home.example.com", token: "wc_pat_secret", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

async function world(opts: { signedIn?: boolean; initial?: SavedServer[]; fetch?: (url: string, init: RequestInit) => Response } = {}) {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of opts.initial ?? []) await store.put(s)
  const f = mockFetch(
    opts.fetch ??
      ((url, init) => {
        const path = new URL(url).pathname
        if (path === "/v1/health") return json(health)
        if (path === "/v1/providers") return json({ providers: [], custom_endpoints: false })
        if (path === "/v1/account/link" && init.method === "POST") return json({ linked: true, issuer: CLOUD })
        if (path === "/v1/push") return new Response(null, { status: 204 })
        return json({ error: "not_found" }, 404)
      }),
  )
  const servers = new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, f))
  const session: AccountLink | null =
    opts.signedIn === undefined
      ? null
      : {
          state: opts.signedIn ? "signedIn" : "signedOut",
          cloudUrl: CLOUD,
          serverToken: async (audience) => ({ token: "t", audience, expires_at: 1 }),
        }
  return { store, servers, session, f }
}

beforeEach(() => history.replaceState(null, "", "/servers/add"))
afterEach(() => {
  history.replaceState(null, "", "/")
  vi.unstubAllGlobals()
})

describe("AddServerView", () => {
  test("tokenFieldIsAPasswordFieldAndTheCodeIsOnlyOfferedSignedIn", async () => {
    const { servers } = await world()
    const { unmount } = render(<AddServerView model={new AddServerModel(servers, null)} />)
    const token = screen.getByLabelText("Personal token") as HTMLInputElement
    expect(token.type).toBe("password")
    expect(token.getAttribute("autocomplete")).toBe("off")
    expect(screen.queryByRole("button", { name: "Pairing code" })).toBeNull()
    expect(screen.getByText(/Create one token per browser/).textContent).toContain("wristcall users tokens add --name browser")
    unmount()

    const signedIn = await world({ signedIn: true })
    render(<AddServerView model={new AddServerModel(signedIn.servers, signedIn.session)} />)
    fireEvent.click(screen.getByRole("button", { name: "Pairing code" }))
    expect(screen.getByLabelText("Pairing code")).toBeTruthy()
    expect(screen.queryByLabelText("Personal token")).toBeNull()
  })

  const office = { id: "c1", name: "Office", url: "https://office.example.com", kind: "self-hosted" as const, linked: false, agents: [], created_at: "", updated_at: "" }

  test("pairingCodeFollowsTheAccountWithoutLeavingTheScreen", async () => {
    const { servers } = await world()
    const listeners = new Set<() => void>()
    const link = {
      state: "signedIn" as "signedIn" | "signedOut",
      cloudUrl: CLOUD,
      serverToken: async (audience: string) => ({ token: "t", audience, expires_at: 1 }),
      onStateChange: (listener: () => void) => {
        listeners.add(listener)
        return () => {
          listeners.delete(listener)
        }
      },
    }
    const { unmount } = render(<AddServerView model={new AddServerModel(servers, link)} />)
    fireEvent.click(screen.getByRole("button", { name: "Pairing code" }))
    expect(screen.getByLabelText("Pairing code")).toBeTruthy()
    act(() => {
      link.state = "signedOut"
      for (const listener of listeners) listener()
    })
    expect(screen.queryByRole("button", { name: "Pairing code" })).toBeNull()
    expect(screen.getByLabelText("Personal token")).toBeTruthy()
    act(() => {
      link.state = "signedIn"
      for (const listener of listeners) listener()
    })
    expect(screen.getByRole("button", { name: "Pairing code" })).toBeTruthy()
    act(() => {
      unmount()
    })
    expect(listeners.size).toBe(0)
  })

  test("setUpRowPrefillsAddressAndNameForAnAccountServerAndStartsWithTheCode", async () => {
    history.replaceState(null, "", "/servers/add?url=https%3A%2F%2FOffice.example.com%3A443%2F&name=Whatever")
    const { servers, session } = await world({ signedIn: true })
    servers.setFromAccount([office])
    render(<AddServerView model={new AddServerModel(servers, session)} />)
    await waitFor(() => expect((screen.getByLabelText("Address") as HTMLInputElement).value).toBe("https://office.example.com"))
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe("Office")
    expect(screen.getByLabelText("Pairing code")).toBeTruthy()
  })

  test("prefillWaitsForTheAccountList", async () => {
    history.replaceState(null, "", "/servers/add?url=https%3A%2F%2Foffice.example.com")
    const { servers, session } = await world({ signedIn: true })
    render(<AddServerView model={new AddServerModel(servers, session)} />)
    expect((screen.getByLabelText("Address") as HTMLInputElement).value).toBe("")
    await act(async () => servers.setFromAccount([office]))
    await waitFor(() => expect((screen.getByLabelText("Address") as HTMLInputElement).value).toBe("https://office.example.com"))
  })

  test("aLinkToAnotherAddressDoesNotFillTheForm", async () => {
    history.replaceState(null, "", "/servers/add?url=https%3A%2F%2Fevil.example&name=Home")
    const { servers, session } = await world({ signedIn: true })
    servers.setFromAccount([office])
    render(<AddServerView model={new AddServerModel(servers, session)} />)
    await act(async () => {
      await servers.load()
    })
    expect((screen.getByLabelText("Address") as HTMLInputElement).value).toBe("")
    expect((screen.getByLabelText(/^Name/) as HTMLInputElement).value).toBe("")
    expect(screen.getByLabelText("Personal token")).toBeTruthy() // still the token method
  })

  test("addingSavesTheServerAndGoesToTheList", async () => {
    const { servers, store, f } = await world()
    vi.stubGlobal("fetch", f)
    render(<App badge={new Badge()} config={{ cloudUrl: null }} servers={servers} />)
    expect(screen.getByRole("heading", { name: "Add server" })).toBeTruthy()
    fireEvent.input(screen.getByLabelText("Address"), { target: { value: "https://home.example.com" } })
    fireEvent.input(screen.getByLabelText("Personal token"), { target: { value: "wc_pat_abc" } })
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add" }))
    })
    await waitFor(() => expect(location.pathname).toBe("/servers"))
    expect((await store.list()).map((s) => s.token)).toEqual(["wc_pat_abc"])
  })

  test("errorStaysOnTheFormAndKeepsWhatWasTyped", async () => {
    const { servers } = await world()
    render(<AddServerView model={new AddServerModel(servers, null)} />)
    fireEvent.input(screen.getByLabelText("Address"), { target: { value: "https://home.example.com" } })
    fireEvent.input(screen.getByLabelText("Personal token"), { target: { value: "not-a-token" } })
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Add" }))
    })
    expect((await screen.findByRole("alert")).textContent).toContain("starts with wc_pat_")
    expect(location.pathname).toBe("/servers/add")
    expect((screen.getByLabelText("Address") as HTMLInputElement).value).toBe("https://home.example.com")
  })
})

describe("ServerDetailView actions", () => {
  test("removeAsksFirstThenForgetsAndLeaves", async () => {
    const { servers, store, f } = await world({ initial: [saved({ pushKey: "wc_push_k" })] })
    history.replaceState(null, "", "/servers/1")
    render(<App badge={new Badge()} config={{ cloudUrl: null }} servers={servers} />)
    await screen.findByLabelText("Name")
    fireEvent.click(screen.getByRole("button", { name: "Remove server" }))
    expect(screen.getByText(/wristcall users tokens revoke/)).toBeTruthy()
    expect(await store.list()).toHaveLength(1)
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }))
    })
    await waitFor(() => expect(location.pathname).toBe("/servers"))
    await servers.cleanupsDone()
    expect(await store.list()).toEqual([])
    expect(f.calls.some((c) => c.url.endsWith("/v1/push") && c.init.method === "DELETE")).toBe(true)
  })

  test("cancelKeepsTheServer", async () => {
    const { servers, store } = await world({ initial: [saved()] })
    history.replaceState(null, "", "/servers/1")
    render(<App badge={new Badge()} config={{ cloudUrl: null }} servers={servers} />)
    await screen.findByLabelText("Name")
    fireEvent.click(screen.getByRole("button", { name: "Remove server" }))
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    expect(screen.getByRole("button", { name: "Remove server" })).toBeTruthy()
    expect(await store.list()).toHaveLength(1)
  })

  test("linkButtonOnlyForAnUnlinkedServerThatAcceptsTheAccount", async () => {
    const { servers, session, store } = await world({ signedIn: true, initial: [saved()] })
    history.replaceState(null, "", "/servers/1")
    render(<App badge={new Badge()} config={{ cloudUrl: CLOUD }} servers={servers} session={session} />)
    const button = await screen.findByRole("button", { name: "Link to account" })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(await screen.findByText("Linked to wristcall account")).toBeTruthy()
    expect(screen.queryByRole("button", { name: /Link to account/ })).toBeNull()
    expect((await store.get("1"))?.linked).toBe(true)
  })

  test("noLinkButtonWithoutASignIn", async () => {
    const { servers } = await world({ initial: [saved()] })
    history.replaceState(null, "", "/servers/1")
    render(<App badge={new Badge()} config={{ cloudUrl: null }} servers={servers} />)
    await screen.findByText("Accepts a wristcall account")
    expect(screen.queryByRole("button", { name: "Link to account" })).toBeNull()
  })

  test("linkErrorIsShown", async () => {
    const { servers, session } = await world({
      signedIn: true,
      initial: [saved()],
      fetch: (url, init) => {
        const path = new URL(url).pathname
        if (path === "/v1/health") return json(health)
        if (path === "/v1/providers") return json({ providers: [], custom_endpoints: false })
        if (path === "/v1/account/link") return json({ error: "conflict" }, 409)
        return json({ error: "not_found" }, 404)
      },
    })
    history.replaceState(null, "", "/servers/1")
    render(<App badge={new Badge()} config={{ cloudUrl: CLOUD }} servers={servers} session={session} />)
    const button = await screen.findByRole("button", { name: "Link to account" })
    await act(async () => {
      fireEvent.click(button)
    })
    expect((await screen.findByRole("alert")).textContent).toBe("Your account is linked to another user of this server.")
  })
})
