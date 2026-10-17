import { act, render, screen, waitFor, within } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test } from "vitest"
import type { CloudServer } from "../api/cloud"
import { ServerClient } from "../api/server"
import { AgentsHomeModel } from "../models/agentsHome"
import { ServersModel } from "../models/servers"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { mockFetch } from "../test/mockFetch"
import { AgentsHome } from "./AgentsHome"

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })
const health = { status: "ok", version: "0.8.2", protocol: 1, account: null, push: null }

function saved(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "s1", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

function agent(id: string, name: string, color?: string) {
  return { id, slug: id, display_name: name, icon: "waveform", call_type: "conversation", position: 0, ...(color === undefined ? {} : { color }) }
}

async function servers(list: SavedServer[], agents: unknown[] = []): Promise<ServersModel> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of list) await store.put(s)
  const fetchMock = mockFetch((u) => {
    if (u.endsWith("/health")) return ok(health)
    if (u.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
    return ok({ agents })
  })
  return new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, fetchMock))
}

beforeEach(() => history.replaceState(null, "", "/agents"))
afterEach(() => history.replaceState(null, "", "/"))

describe("AgentsHome", () => {
  test("AgentsHome rows wear the agent color class", async () => {
    const model = await servers([saved()], [agent("ag_a", "Alpha", "indigo"), agent("ag_b", "Beta"), agent("ag_c", "Gamma", "unheard-of")])
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    const list = await screen.findByRole("list", { name: "home agents" })
    const rows = within(list).getAllByRole("link")
    expect(rows.map((r) => r.querySelector(".agent-disc")!.className)).toEqual([
      "agent-disc color-indigo",
      "agent-disc color-mint",
      "agent-disc color-mint",
    ])
    expect(rows[0]!.getAttribute("href")).toBe("/servers/s1/agents/ag_a")
    expect(screen.getByRole("heading", { name: "home" })).toBeTruthy()
    expect(screen.getByRole("link", { name: "New agent" }).getAttribute("href")).toBe("/servers/s1/agents/new")
    expect(document.querySelector("[style]")).toBeNull()
  })

  test("empty home shows the iPhone hint", async () => {
    const model = await servers([])
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    expect(await screen.findByText("No agents yet.")).toBeTruthy()
    expect(screen.getByText("Add one in wristcall on your iPhone, or connect your own server in Settings › Servers.")).toBeTruthy()
  })

  test("a failing server shows its problem and the others stay", async () => {
    const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
    await store.put(saved({ id: "s1", name: "home" }))
    await store.put(saved({ id: "s2", name: "office", url: "https://office.example.com", addedAt: 2 }))
    const m = new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, mockFetch((u) => {
      if (u.includes("home.example.com") && u.endsWith("/agents")) throw new TypeError("offline")
      if (u.endsWith("/health")) return ok(health)
      if (u.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
      return ok({ agents: [agent("b", "Beta", "teal")] })
    })))
    render(<AgentsHome model={new AgentsHomeModel(m)} servers={m} />)
    expect(await screen.findByText("Beta")).toBeTruthy()
    expect(screen.getByRole("alert")).toBeTruthy()
    expect(screen.queryByText("No agents yet.")).toBeNull()
  })

  test("account servers show their agents read-only", async () => {
    const model = await servers([])
    const entry: CloudServer = {
      id: "c1", name: "Office", url: "https://office.example.com", kind: "self-hosted", linked: false, created_at: "", updated_at: "",
      agents: [{ id: "x", slug: "x", display_name: "Xena", icon: "waveform", call_type: "conversation", color: "pink" }],
    }
    render(<AgentsHome model={new AgentsHomeModel(model)} servers={model} />)
    await screen.findByText("No agents yet.")
    act(() => model.setFromAccount([entry]))
    expect(await screen.findByText("Xena")).toBeTruthy()
    expect(screen.queryByText("No agents yet.")).toBeNull()
    const xena = screen.getByText("Xena").closest("li")!
    expect(xena.querySelector("a")).toBeNull()
    expect(xena.querySelector(".agent-disc")!.className).toBe("agent-disc color-pink")
    expect(screen.queryByRole("link", { name: "New agent" })).toBeNull()
    expect(screen.getByRole("link", { name: "Set up" }).getAttribute("href")).toBe("/settings/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office")
    await waitFor(() => expect(screen.getByRole("heading", { name: "Office" })).toBeTruthy())
  })
})
