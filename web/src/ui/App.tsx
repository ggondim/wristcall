import type { AccountModel } from "../models/account"
import { AddServerModel } from "../models/addServer"
import { ApprovalsModel } from "../models/approvals"
import type { AccountLink, ServersModel } from "../models/servers"
import { SettingsView } from "./AccountView"
import { AddServerView } from "./AddServerView"
import { AgentsScreen } from "./AgentsView"
import { ApproveWatchSignInView } from "./ApproveWatchSignInView"
import { ApprovalsView } from "./ApprovalsView"
import { CallbackView } from "./CallbackView"
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
  servers: ServersModel
  /** The account sign-in; null when the site runs without accounts. */
  session?: AccountLink | null
  /** The account screens (Settings, the sign-in callback); null when the site runs without accounts. */
  account?: AccountModel | null
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
  servers,
  approvals,
  session,
  account,
}: {
  route: Route
  servers: ServersModel
  approvals: ApprovalsModel
  session: AccountLink | null
  account: AccountModel | null
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
  if (route.tab === "callback") return <CallbackView model={account} />
  if (route.tab === "settings") {
    if (route.path === "/settings/watch" && account !== null) return <ApproveWatchSignInView model={account} />
    return <SettingsView account={account} />
  }
  return (
    <main class="screen">
      <h1>{titleOf(route)}</h1>
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

export function App({ badge, approvals: given, servers, session = null, account = null }: AppProps) {
  const [route, setRoute] = useState<Route>(() => parseRoute(location.pathname))
  const [search, setSearch] = useState(() => location.search)
  useEffect(() => {
    const read = () => {
      const next = parseRoute(location.pathname)
      setRoute((now) => (now.tab === next.tab && now.path === next.path ? now : next))
      setSearch(location.search)
    }
    const off = onNavigate(read)
    read() // a screen's own effect (they run first) may have moved the address before this subscription existed
    return off
  }, [])
  const approvals = useMemo(() => given ?? new ApprovalsModel(servers), [given, servers])
  // A notification opens `/approvals?server=&request=&expires=`: the model checks the request and highlights it.
  useEffect(() => {
    if (route.tab !== "approvals") return
    const target = parseApprovalTarget(search)
    if (target === null) return
    void approvals.focus(target.serverId, target.requestId, target.expiresAt)
    // Read once: a relaunch or reload of this address must not focus the same request again.
    history.replaceState(null, "", "/approvals")
  }, [route, search, approvals])
  return (
    <>
      <Screen route={route} servers={servers} approvals={approvals} session={session} account={account} />
      {route.tab === "callback" ? null : <Tabs route={route} badge={badge} />}
    </>
  )
}
