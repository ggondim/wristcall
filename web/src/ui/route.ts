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
