import { act, fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import type { CloudServer } from "../api/cloud"
import { ServerClient } from "../api/server"
import { ServersModel, unavailableStorage } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { ServerDetailView } from "./ServerDetailView"
import { ServersView } from "./ServersView"

function server(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "1", name: "home", url: "https://home.example.com", token: "wc_pat_secret", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
const health = (account: object | null) => ({ status: "ok", version: "0.7.0", protocol: 1, account, push: null })

async function modelWith(saved: SavedServer[], account: object | null = null): Promise<ServersModel> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of saved) await store.put(s)
  return new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, mockFetch((u) => (u.endsWith("/health") ? ok(health(account)) : ok({ providers: [], custom_endpoints: false })))))
}

beforeEach(() => history.replaceState(null, "", "/servers"))
afterEach(() => history.replaceState(null, "", "/"))

describe("ServersView", () => {
  test("servers empty state has no terminal hint", async () => {
    render(<ServersView model={await modelWith([])} />)
    await screen.findByText("No servers yet.")
    expect(screen.getByRole("link", { name: "Add server" }).getAttribute("href")).toBe("/settings/servers/add")
    expect(document.body.textContent).not.toMatch(/terminal|wristcall users|command|token/i)
  })

  test("serversViewListsRows", async () => {
    const model = await modelWith(
      [server({ id: "1", name: "home", linked: true }), server({ id: "2", name: "office", url: "https://office.example.com:8443", addedAt: 2 })],
      { issuer: "https://id.example.com", device_credential: "x" },
    )
    render(<ServersView model={model} />)
    await screen.findByText("home")
    await waitFor(() => expect(screen.getAllByText("Version 0.7.0")).toHaveLength(2))
    expect(screen.getByText("office.example.com:8443")).toBeTruthy()
    // Only the linked server shows the badge, though both accept an account.
    expect(screen.getAllByText("Account")).toHaveLength(1)
    expect(screen.getByRole("link", { name: /home/ }).getAttribute("href")).toBe("/servers/1")
  })

  test("serversViewShowsProblemAndAccountServers", async () => {
    const down = new ServersModel(
      { list: async () => [server()], get: async () => null, put: async () => {}, remove: async () => {}, update: async () => null },
      () => new ServerClient(new URL("https://home.example.com"), "t", mockFetch(() => { throw new TypeError("offline") })),
    )
    render(<ServersView model={down} />)
    await screen.findByText("Can't reach host.")
    act(() => down.setFromAccount([{ id: "c", name: "Office", url: "https://office.example.com", kind: "self-hosted", linked: false, agents: [], created_at: "", updated_at: "" } satisfies CloudServer]))
    expect(screen.getByText("From your account")).toBeTruthy()
    const setUp = screen.getByRole("link", { name: /Office/ })
    expect(setUp.getAttribute("href")).toBe("/settings/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office")
  })
})

describe("ServersView without storage", () => {
  test("showsTheStorageFailureInsteadOfABlankPage", async () => {
    render(<ServersView model={new ServersModel(unavailableStorage)} />)
    await screen.findByText("The saved servers could not be read.")
    expect(screen.getByRole("link", { name: "Add server" })).toBeTruthy()
  })
})

describe("ServerDetailView", () => {
  test("renamesOnBlurAndShowsRefusals", async () => {
    const model = await modelWith([server()])
    render(<ServerDetailView model={model} id="1" />)
    const input = (await screen.findByLabelText("Name")) as HTMLInputElement
    expect(screen.getByText("https://home.example.com")).toBeTruthy()
    fireEvent.input(input, { target: { value: "  " } })
    fireEvent.blur(input)
    await screen.findByText("The name cannot be empty.")
    expect(input.value).toBe("home")
    fireEvent.input(input, { target: { value: "office" } })
    fireEvent.blur(input)
    await waitFor(() => expect(screen.getByRole("heading", { name: "office" })).toBeTruthy())
    expect(screen.queryByText("The name cannot be empty.")).toBeNull()
  })

  test("enterThenBlurRenamesOnceAndSkipsTrimmedEqualNames", async () => {
    const model = await modelWith([server()])
    let renames = 0
    const rename = model.rename.bind(model)
    model.rename = async (id, name) => {
      renames++
      await rename(id, name)
    }
    render(<ServerDetailView model={model} id="1" />)
    const input = (await screen.findByLabelText("Name")) as HTMLInputElement
    fireEvent.input(input, { target: { value: "  home  " } })
    fireEvent.blur(input)
    await waitFor(() => expect(input.value).toBe("home"))
    expect(renames).toBe(0)
    fireEvent.input(input, { target: { value: "office" } })
    fireEvent.submit(input.form!)
    fireEvent.blur(input)
    await waitFor(() => expect(screen.getByRole("heading", { name: "office" })).toBeTruthy())
    expect(renames).toBe(1)
  })

  test("linksToManagementScreens", async () => {
    render(<ServerDetailView model={await modelWith([server()])} id="1" />)
    await screen.findByText("Agents")
    expect(screen.getByRole("link", { name: "Agents" }).getAttribute("href")).toBe("/servers/1/agents")
    expect(screen.getByRole("link", { name: "Devices" }).getAttribute("href")).toBe("/servers/1/devices")
    expect(screen.getByRole("link", { name: "History" }).getAttribute("href")).toBe("/history/1")
  })

  test("unknownServerSaysRemoved", async () => {
    render(<ServerDetailView model={await modelWith([])} id="nope" />)
    await screen.findByText("Server removed")
  })
})
