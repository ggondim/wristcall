import type { AppConfig } from "../config"
import { AddServerModel } from "../models/addServer"
import { ApprovalsModel } from "../models/approvals"
import type { AccountLink, ServersModel } from "../models/servers"
import { AddServerView } from "./AddServerView"
import { AgentsScreen } from "./AgentsView"
import { ApprovalsView } from "./ApprovalsView"
import { DevicesScreen } from "./DevicesView"
import { ServerDetailView } from "./ServerDetailView"
import { ServersView } from "./ServersView"
import { onNavigate, parseAgentsTarget, parseApprovalTarget, parseDevicesTarget, parseRoute, type Route } from "./route"
import { Tabs } from "./Tabs"
import { useEffect, useMemo, useState } from "preact/hooks"
import type { Model } from "./useModel"

export { navigate, parseRoute, type Route } from "./route"

export interface AppProps {
  /** Pending approval requests, shown on the Servers tab. */
  badge: Model<{ count: number }>
  /** The requests behind the badge; the approvals route opens from the badge or a notification. Made over `servers` when left out. */
  approvals?: ApprovalsModel
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
  approvals,
  session,
}: {
  route: Route
  config: AppConfig
  servers: ServersModel
  approvals: ApprovalsModel
  session: AccountLink | null
}) {
  const addServer = useMemo(() => new AddServerModel(servers, session), [servers, session])
  if (route.tab === "servers") {
    if (route.path === "/" || route.path === "/servers") return <ServersView model={servers} />
    if (route.path === "/servers/add") return <AddServerView model={addServer} />
    const agents = parseAgentsTarget(route.path)
    if (agents !== null) return <AgentsScreen servers={servers} target={agents} />
    const devices = parseDevicesTarget(route.path)
    if (devices !== null) return <DevicesScreen servers={servers} approvals={approvals} target={devices} />
    return <ServerDetailView model={servers} id={decodeId(route.path.slice("/servers/".length))} account={session} />
  }
  if (route.tab === "approvals") return <ApprovalsView model={approvals} />
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

export function App({ badge, approvals: given, config, servers, session = null }: AppProps) {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname))
  const [search, setSearch] = useState(() => location.search)
  useEffect(
    () =>
      onNavigate(() => {
        setRoute(parseRoute(location.pathname))
        setSearch(location.search)
      }),
    [],
  )
  const approvals = useMemo(() => given ?? new ApprovalsModel(servers), [given, servers])
  // A notification opens `/approvals?server=&request=&expires=`: the model checks the request and highlights it.
  useEffect(() => {
    if (route.tab !== "approvals") return
    const target = parseApprovalTarget(search)
    if (target !== null) void approvals.focus(target.serverId, target.requestId, target.expiresAt)
  }, [route, search, approvals])
  return (
    <>
      <Screen route={route} config={config} servers={servers} approvals={approvals} session={session} />
      {route.tab === "callback" ? null : <Tabs route={route} badge={badge} />}
    </>
  )
}
