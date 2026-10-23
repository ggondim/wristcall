import type { CloudAgent, CloudRoute, CloudServer } from "../api/cloud"
import { ServerClient } from "../api/server"
import { ServersModel } from "../models/servers"
import type { SavedServer } from "../store/servers"
import { mockFetch, type MockFetch } from "./mockFetch"

export const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })

export function savedServer(over: Partial<SavedServer> = {}): SavedServer {
  return { id: "home", name: "home", url: "https://home.example.com", token: "wc_pat_secret", linked: true, cloudServerId: null, pushKey: null, pushEndpoint: null, addedAt: 1, ...over }
}

/** What a fake server does for one request; null falls through to the defaults (health, providers, 404). */
export type Handler = (path: string, method: string) => Response | Promise<Response> | null

/** Servers over an in-memory store, all answered by one `fetch` that routes by host. `accounts` lists the hosts that have one. */
export function serversWith(
  saved: SavedServer[],
  handlers: Record<string, Handler> = {},
  accounts: string[] = saved.map((server) => new URL(server.url).host),
): { servers: ServersModel; fetchMock: MockFetch } {
  const fetchMock = mockFetch((url, init) => {
    const parsed = new URL(url)
    const path = parsed.pathname.replace(/^\/v1\//, "")
    const answer = handlers[parsed.host]?.(path, init.method ?? "GET")
    if (answer) return answer
    if (path === "health") {
      const account = accounts.includes(parsed.host) ? { issuer: "https://cloud.example.com", device_credential: "x" } : null
      return json({ status: "ok", version: "0.7.0", protocol: 1, account, push: null })
    }
    if (path === "providers") return json({ providers: [], custom_endpoints: false })
    return json({ error: "not_found", message: "no such route" }, 404)
  })
  const store = {
    list: async () => saved,
    get: async (id: string) => saved.find((server) => server.id === id) ?? null,
    put: async () => {},
    remove: async () => {},
    update: async () => null,
  }
  const servers = new ServersModel(store, (server) => new ServerClient(new URL(server.url), server.token, fetchMock))
  return { servers, fetchMock }
}

/** The host of the Wristcall Cloud entry's node in `attachCloudRoute`: answer it with `handlers[NODE]` of `serversWith`. */
export const NODE = "node.example.com"

export function cloudAgent(id: string, name: string): CloudAgent {
  return { id, slug: id, display_name: name, icon: "waveform", call_type: "conversation" }
}

/** The account's Wristcall Cloud entry, as the agenda lists it. */
export function cloudEntry(over: Partial<CloudServer> = {}): CloudServer {
  return { id: "srv_0123456789ab", name: "Wristcall Cloud", url: "https://cloud.example.com", kind: "cloud", linked: true, agents: [], created_at: "", updated_at: "", ...over }
}

/**
 * Gives `servers` a router whose `manage` route is `NODE`, answered by the same `fetchMock`. Set `route.fail` to make the
 * route fail with that error (a Cloud with no node, say), and back to null to make it answer again.
 */
export function attachCloudRoute(servers: ServersModel, fetchMock: MockFetch): { fail: unknown } {
  const route: { fail: unknown } = { fail: null }
  const reply: CloudRoute = { node: { id: "node-1", url: `https://${NODE}`, region: "sao" }, token: "tok-1", expires_at: 4_000_000_000, fallback: null }
  servers.attachRouter(
    {
      manage: async () => {
        if (route.fail !== null) throw route.fail
        return reply
      },
      invalidate: () => {},
    },
    { fetchImpl: fetchMock },
  )
  return route
}
