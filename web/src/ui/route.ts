export type Route =
  | { tab: "servers"; path: "/" | "/servers" }
  | { tab: "servers"; path: "/servers/add" }
  | { tab: "servers"; path: `/servers/${string}` }
  | { tab: "history"; path: "/history" | `/history/${string}` }
  | { tab: "approvals"; path: "/approvals" }
  | { tab: "settings"; path: "/settings" | "/settings/watch" }
  | { tab: "callback"; path: "/auth/callback" }

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
  if (rest.length > 0) return FALLBACK
  switch (path) {
    case "/":
      return { tab: "servers", path: "/" }
    case "/servers":
      return { tab: "servers", path: "/servers" }
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
