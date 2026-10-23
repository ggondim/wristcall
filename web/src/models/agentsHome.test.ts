import { describe, expect, test, vi } from "vitest"
import type { CloudServer } from "../api/cloud"
import { ServerClient } from "../api/server"
import { openDb } from "../store/db"
import { ServerStore, type SavedServer } from "../store/servers"
import { ApiError } from "../lib/errors"
import { attachCloudRoute, cloudAgent, cloudEntry, json, NODE, savedServer, serversWith } from "../test/fakeServers"
import { mockFetch } from "../test/mockFetch"
import { resolvedColor, AGENT_COLORS } from "../ui/agentColor"
import { AgentsHomeModel } from "./agentsHome"
import { ServersModel } from "./servers"

function saved(over: Partial<SavedServer>): SavedServer {
  return { id: "1", name: "home", url: "https://home.example.com", token: "wc_pat_x", linked: false, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

function agent(id: string, name: string, color?: string | null) {
  return { id, slug: id, display_name: name, icon: "waveform", call_type: "conversation", position: 0, ...(color === undefined ? {} : { color }) }
}

const ok = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } })

/** A servers model over `servers`; `handler(host, path)` answers every request. */
async function modelWith(servers: SavedServer[], handler: (host: string, path: string) => Response): Promise<ServersModel> {
  const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
  for (const s of servers) await store.put(s)
  return new ServersModel(store, (s) => new ServerClient(new URL(s.url), s.token, mockFetch((u) => handler(new URL(u).host, new URL(u).pathname))))
}

const health = { status: "ok", version: "0.8.2", protocol: 1, account: null, push: null }

describe("resolvedColor", () => {
  test("resolvedColor falls back to mint", () => {
    expect(resolvedColor(undefined)).toBe("mint")
    expect(resolvedColor(null)).toBe("mint")
    expect(resolvedColor("chartreuse")).toBe("mint")
    expect(resolvedColor(7)).toBe("mint")
    for (const name of AGENT_COLORS) expect(resolvedColor(name)).toBe(name)
  })
})

describe("AgentsHomeModel", () => {
  test("loads agents from every server", async () => {
    const servers = await modelWith(
      [saved({ id: "1", name: "home" }), saved({ id: "2", name: "office", url: "https://office.example.com", addedAt: 2 })],
      (host, path) => {
        if (path.endsWith("/health")) return ok(health)
        if (path.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
        return ok({ agents: host === "home.example.com" ? [agent("a", "Alpha", "red")] : [agent("b", "Beta"), agent("c", "Gamma", "blue")] })
      },
    )
    const model = new AgentsHomeModel(servers)
    await model.load()
    expect(model.state.loaded).toBe(true)
    expect(model.state.sections.map((s) => [s.serverName, s.agents.map((a) => a.display_name), s.problem])).toEqual([
      ["home", ["Alpha"], null],
      ["office", ["Beta", "Gamma"], null],
    ])
  })

  test("a server that fails keeps the others", async () => {
    const servers = await modelWith(
      [saved({ id: "1", name: "home" }), saved({ id: "2", name: "office", url: "https://office.example.com", addedAt: 2 })],
      (host, path) => {
        if (host === "home.example.com" && path.endsWith("/agents")) throw new TypeError("offline")
        if (path.endsWith("/health")) return ok(health)
        if (path.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
        return ok({ agents: [agent("b", "Beta")] })
      },
    )
    const model = new AgentsHomeModel(servers)
    await model.load()
    const [home, office] = model.state.sections
    expect(home!.agents).toEqual([])
    expect(home!.problem).toBeTruthy()
    expect(office!.agents.map((a) => a.display_name)).toEqual(["Beta"])
    expect(office!.problem).toBeNull()
    expect(model.state.loaded).toBe(true)
  })

  test("a hung server does not delay the others", async () => {
    const never = new Promise<Response>(() => {})
    const store = new ServerStore(await openDb("t-" + crypto.randomUUID()))
    await store.put(saved({ id: "1", name: "home", url: "https://hung.example.com" }))
    await store.put(saved({ id: "2", name: "office", url: "https://office.example.com", addedAt: 2 }))
    const servers = new ServersModel(store, (s) =>
      new ServerClient(new URL(s.url), s.token, mockFetch((u) => {
        if (new URL(u).host === "hung.example.com") return never
        if (u.endsWith("/health")) return ok(health)
        if (u.endsWith("/providers")) return ok({ providers: [], custom_endpoints: false })
        return ok({ agents: [agent("b", "Beta")] })
      })),
    )
    const model = new AgentsHomeModel(servers)
    void model.load()
    await vi.waitFor(() => expect(model.state.sections.map((s) => s.serverName)).toEqual(["office"]))
    expect(model.state.sections[0]!.agents.map((a) => a.display_name)).toEqual(["Beta"])
    expect(model.state.loaded).toBe(false)
  })

  test("account servers follow the agenda after the load", async () => {
    const servers = await modelWith([], () => ok({}))
    const model = new AgentsHomeModel(servers)
    await model.load()
    expect(model.state.sections).toEqual([])
    const entry: CloudServer = { id: "c1", name: "Office", url: "https://office.example.com", kind: "self-hosted", linked: false, agents: [{ id: "x", slug: "x", display_name: "Xena", icon: "waveform", call_type: "conversation", color: "pink" }], created_at: "", updated_at: "" }
    servers.setFromAccount([entry])
    model.refreshAccount()
    const [section] = model.state.sections
    expect(section!.serverName).toBe("Office")
    expect(section!.agents.map((a) => a.display_name)).toEqual(["Xena"])
    expect(section!.setupHref).toBe("/settings/servers/add?url=https%3A%2F%2Foffice.example.com&name=Office")
  })
})

describe("Wristcall Cloud section", () => {
  test("cloud section comes first with node agents", async () => {
    const { servers, fetchMock } = serversWith([savedServer({ id: "home", name: "home" })], {
      "home.example.com": (path) => (path === "agents" ? json({ agents: [agent("h", "Kitchen")] }) : null),
      [NODE]: (path) => (path === "agents" ? json({ agents: [agent("n2", "Notes", "teal"), { ...agent("n1", "Assistant"), position: -1 }] }) : null),
    })
    attachCloudRoute(servers, fetchMock)
    servers.setCloud(cloudEntry({ agents: [cloudAgent("old", "From the agenda")] }))
    const model = new AgentsHomeModel(servers)
    await model.load()
    expect(model.state.sections.map((s) => [s.serverId, s.serverName])).toEqual([
      ["cloud", "Wristcall Cloud"],
      ["home", "home"],
    ])
    const cloud = model.state.sections[0]!
    // The node's own list, in its order: not the agenda's copy.
    expect(cloud.agents.map((a) => a.display_name)).toEqual(["Assistant", "Notes"])
    expect(cloud.problem).toBeNull()
    expect(cloud.setupHref).toBeNull()
    expect(model.state.loaded).toBe(true)
    // The entry leaving the agenda (signed out, say) takes its section along.
    servers.setCloud(null)
    model.refreshAccount()
    expect(model.state.sections.map((s) => s.serverId)).toEqual(["home"])
  })

  test("cloud section falls back to agenda agents when the route fails", async () => {
    const { servers, fetchMock } = serversWith([], { [NODE]: (path) => (path === "agents" ? json({ agents: [agent("n1", "From the node")] }) : null) })
    const route = attachCloudRoute(servers, fetchMock)
    const model = new AgentsHomeModel(servers)
    await model.load()
    expect(model.state.sections).toEqual([])
    // The entry arrives after the first load (the agenda is read later): its section follows.
    servers.setCloud(cloudEntry({ agents: [cloudAgent("a1", "Old agenda agent")] }))
    model.refreshAccount()
    await vi.waitFor(() => expect(model.state.sections[0]?.agents.map((a) => a.display_name)).toEqual(["From the node"]))
    // The node cannot be reached now: the agenda's agents, never the ones shown before (they may be another account's).
    route.fail = new ApiError(503, "no_node_available", "No node can take this account right now.", 30)
    servers.setCloud(cloudEntry({ agents: [cloudAgent("a2", "Agenda agent")] }))
    await model.load()
    const cloud = model.state.sections[0]!
    expect(cloud.serverId).toBe("cloud")
    expect(cloud.agents.map((a) => a.display_name)).toEqual(["Agenda agent"])
    expect(cloud.problem).toBe("Wristcall Cloud is temporarily unavailable. Try again in a minute.")
    expect(cloud.setupHref).toBeNull()
  })
})
