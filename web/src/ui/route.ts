export type Route =
  | { tab: "servers"; path: "/" | "/servers" | "/agents" }
  | { tab: "servers"; path: "/servers/add" }
  | { tab: "servers"; path: `/servers/${string}` }
  | { tab: "history"; path: "/history" | `/history/${string}` }
  | { tab: "approvals"; path: "/approvals" }
  | { tab: "settings"; path: "/settings" | "/settings/watch" | "/settings/servers/add" }
  | { tab: "callback"; path: "/auth/callback" }
  | { tab: "link"; path: "/link" }
  | { tab: "signIn"; path: "/sign-in" }

const FALLBACK: Route = { tab: "servers", path: "/servers" }
const NAVIGATE_EVENT = "wristcall:navigate"

/** Path (History API) to route; anything unknown is the servers list. */
export function parseRoute(pathname: string): Route {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, "") : pathname
  const parts = path.split("/").slice(1)
  const [head, id, ...rest] = parts
  // /servers/:id/agents, /servers/:id/agents/new and /servers/:id/agents/:agent
  if (head === "servers" && id && rest[0] === "agents" && rest.length <= 2 && rest[1] !== "") {
    return { tab: "servers", path: `/servers/${parts.slice(1).join("/")}` }
  }
  // /servers/:id/devices and /servers/:id/devices/code
  if (head === "servers" && id && rest[0] === "devices" && (rest.length === 1 || (rest.length === 2 && rest[1] === "code"))) {
    return { tab: "servers", path: `/servers/${parts.slice(1).join("/")}` }
  }
  // /history/:server/:call
  if (head === "history" && id && rest.length === 1 && rest[0] !== "") return { tab: "history", path: `/history/${id}/${rest[0]}` }
  // Where the sign-in screen's "own server" link goes (Task 5 moves Servers under Settings).
  if (path === "/settings/servers/add") return { tab: "settings", path: "/settings/servers/add" }
  if (rest.length > 0) return FALLBACK
  switch (path) {
    case "/":
      return { tab: "servers", path: "/" }
    case "/servers":
      return { tab: "servers", path: "/servers" }
    case "/agents":
      return { tab: "servers", path: "/agents" } // provisional: the servers list until Task 5 brings the Agents tab
    case "/servers/add":
      return { tab: "servers", path: "/servers/add" }
    case "/history":
      return { tab: "history", path: "/history" }
    case "/approvals":
      return { tab: "approvals", path: "/approvals" }
    case "/settings":
      return { tab: "settings", path: "/settings" }
    case "/settings/watch":
      return { tab: "settings", path: "/settings/watch" }
    case "/auth/callback":
      return { tab: "callback", path: "/auth/callback" }
    case "/link":
      return { tab: "link", path: "/link" }
    case "/sign-in":
      return { tab: "signIn", path: "/sign-in" }
  }
  if (head === "servers" && id) return { tab: "servers", path: `/servers/${id}` }
  if (head === "history" && id) return { tab: "history", path: `/history/${id}` }
  return FALLBACK
}

/** pushState plus an event, so the App re-reads the location (popstate only fires for the back and forward buttons). */
export function navigate(path: string): void {
  history.pushState(null, "", path)
  window.dispatchEvent(new Event(NAVIGATE_EVENT))
}

/** Like `navigate`, but replaces the current history entry (the callback address must not stay in the back stack). */
export function replaceRoute(path: string): void {
  history.replaceState(null, "", path)
  window.dispatchEvent(new Event(NAVIGATE_EVENT))
}

/** Calls `listener` on every navigation, by the back button or by navigate(). Returns the unsubscribe. */
export function onNavigate(listener: () => void): () => void {
  window.addEventListener("popstate", listener)
  window.addEventListener(NAVIGATE_EVENT, listener)
  return () => {
    window.removeEventListener("popstate", listener)
    window.removeEventListener(NAVIGATE_EVENT, listener)
  }
}

/** Where an agents screen is: the list, the form of a new agent, or the form of one agent. Ids are decoded. */
export type AgentsTarget = { serverId: string; screen: "list" } | { serverId: string; screen: "new" } | { serverId: string; screen: "edit"; agentId: string }

function decoded(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function agentsPath(serverId: string): string {
  return `/servers/${encodeURIComponent(serverId)}/agents`
}

/** The agents screen a path under `/servers/` names, or null for any other server path. */
export function parseAgentsTarget(path: string): AgentsTarget | null {
  const [head, id, section, agent, ...rest] = path.split("/").slice(1)
  if (head !== "servers" || !id || section !== "agents" || rest.length > 0) return null
  const serverId = decoded(id)
  if (agent === undefined) return { serverId, screen: "list" }
  if (agent === "new") return { serverId, screen: "new" }
  return agent === "" ? null : { serverId, screen: "edit", agentId: decoded(agent) }
}

/** Where a devices screen is: the list of devices, or the pairing code. The id is decoded. */
export type DevicesTarget = { serverId: string; screen: "list" | "code" }

export function devicesPath(serverId: string): string {
  return `/servers/${encodeURIComponent(serverId)}/devices`
}

/** The devices screen a path under `/servers/` names, or null for any other server path. */
export function parseDevicesTarget(path: string): DevicesTarget | null {
  const [head, id, section, screen, ...rest] = path.split("/").slice(1)
  if (head !== "servers" || !id || section !== "devices" || rest.length > 0) return null
  if (screen === undefined) return { serverId: decoded(id), screen: "list" }
  return screen === "code" ? { serverId: decoded(id), screen: "code" } : null
}

/** What a notification put in the approvals address: `?server=<local id>&request=<4 digits>&expires=<Unix seconds>`. */
export interface ApprovalTarget {
  serverId: string
  requestId: string
  expiresAt: number | null
}

/** The target of `/approvals?...`, or null when the address names no server and request. Nothing else of it is used. */
export function parseApprovalTarget(search: string): ApprovalTarget | null {
  const params = new URLSearchParams(search)
  const serverId = params.get("server")
  const requestId = params.get("request")
  if (!serverId || !requestId) return null
  const raw = params.get("expires")
  const expires = raw === null || raw.trim() === "" ? null : Number(raw)
  return { serverId, requestId, expiresAt: expires !== null && Number.isFinite(expires) ? expires : null }
}

/** The call a history detail path names. Ids are decoded. */
export interface HistoryTarget {
  serverId: string
  callId: string
}

export function historyPath(serverId: string, callId: string): string {
  return `/history/${encodeURIComponent(serverId)}/${encodeURIComponent(callId)}`
}

/** The detail screen `/history/<server>/<call>` names, or null for the list and anything else. */
export function parseHistoryTarget(path: string): HistoryTarget | null {
  const [head, server, call, ...rest] = path.split("/").slice(1)
  if (head !== "history" || !server || !call || rest.length > 0) return null
  return { serverId: decoded(server), callId: decoded(call) }
}

/** The server a `/history/<server>` address names (the "History" link of a server's detail screen), or null for anything else. */
export function parseHistoryServer(path: string): string | null {
  const [head, server, ...rest] = path.split("/").slice(1)
  if (head !== "history" || !server || rest.length > 0) return null
  return decoded(server)
}
