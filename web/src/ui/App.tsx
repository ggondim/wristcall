import type { AppConfig } from "../config"
import { AddServerModel } from "../models/addServer"
import type { AccountLink, ServersModel } from "../models/servers"
import { AddServerView } from "./AddServerView"
import { ServerDetailView } from "./ServerDetailView"
import { ServersView } from "./ServersView"
import { onNavigate, parseRoute, type Route } from "./route"
import { Tabs } from "./Tabs"
import { useEffect, useMemo, useState } from "preact/hooks"
import type { Model } from "./useModel"

export { navigate, parseRoute, type Route } from "./route"

export interface AppProps {
  /** Pending approval requests, shown on the Servers tab. */
  badge: Model<{ count: number }>
  config: AppConfig
  servers: ServersModel
  /** The account sign-in; null when the site runs without accounts. */
  session?: AccountLink | null
}

function titleOf(route: Route): string {
  switch (route.tab) {
    case "servers":
      return "Servers"
    case "approvals":
      return "Approvals"
    case "history":
      return "History"
    case "settings":
      return "Settings"
    case "callback":
      return "Signing in"
  }
}

// The screens arrive with their own tasks; each route renders its title until then.
function Screen({
  route,
  config,
  servers,
  session,
}: {
  route: Route
  config: AppConfig
  servers: ServersModel
  session: AccountLink | null
}) {
  const addServer = useMemo(() => new AddServerModel(servers, session), [servers, session])
  if (route.tab === "servers") {
    if (route.path === "/" || route.path === "/servers") return <ServersView model={servers} />
    if (route.path === "/servers/add") return <AddServerView model={addServer} />
    return <ServerDetailView model={servers} id={decodeId(route.path.slice("/servers/".length))} account={session} />
  }
  return (
    <main class="screen">
      <h1>{titleOf(route)}</h1>
      {route.tab === "settings" ? <p>{config.cloudUrl ? "Account sign-in is available." : "Account sign-in is not configured."}</p> : null}
    </main>
  )
}

function decodeId(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function App({ badge, config, servers, session = null }: AppProps) {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname))
  useEffect(() => onNavigate(() => setRoute(parseRoute(location.pathname))), [])
  return (
    <>
      <Screen route={route} config={config} servers={servers} session={session} />
      {route.tab === "callback" ? null : <Tabs route={route} badge={badge} />}
    </>
  )
}
