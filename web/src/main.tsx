import { render } from "preact"
import "./ui/styles.css"
import { AgendaSync } from "./account/agenda"
import { AccountSession } from "./account/session"
import { CloudClient } from "./api/cloud"
import { loadConfig } from "./config"
import { AccountModel } from "./models/account"
import { ApprovalsBadge, ApprovalsModel, refreshWhenServersChange } from "./models/approvals"
import { openServerStorage, ServersModel } from "./models/servers"
import { ServerStore } from "./store/servers"
import { App } from "./ui/App"

/** The agenda's servers this browser has no token for ("From your account"), read now and when the account or the page changes. */
function showAccountServers(agenda: AgendaSync, servers: ServersModel, session: AccountSession): void {
  const refresh = (): void => {
    void agenda.fromAccount().then((entries) => servers.setFromAccount(entries))
  }
  refresh()
  session.onStateChange(refresh)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh()
  })
}

async function start(): Promise<void> {
  const config = await loadConfig()
  const storage = await openServerStorage()
  const servers = new ServersModel(storage)
  const approvals = new ApprovalsModel(servers)
  // The account needs the Cloud from /config.json (never from a server) and a database that opened.
  let session: AccountSession | null = null
  let cloud: CloudClient | null = null
  if (config.cloudUrl !== null && storage instanceof ServerStore) {
    cloud = new CloudClient(config.cloudUrl)
    session = new AccountSession(cloud, storage)
    await session.load().catch(() => {}) // unreadable: signed out
  }
  // The agenda mirror: every change of the servers (and of an agents list) is queued for the account's agenda.
  const agenda = session === null || !(storage instanceof ServerStore) ? null : new AgendaSync(session, servers, storage)
  if (agenda !== null) servers.onChange((event) => agenda.serverChanged(event))
  // The push keys join the account in their own task.
  const account = session === null ? null : new AccountModel(session, agenda, null, { cloud, servers })
  render(
    <App
      account={account}
      approvals={approvals}
      badge={new ApprovalsBadge(approvals)}
      servers={servers}
      session={session}
      onAgentsChanged={agenda === null ? undefined : (serverId, agents) => agenda.agentsChanged(serverId, agents)}
    />,
    document.getElementById("app")!,
  )
  account?.start()
  if (agenda !== null && session !== null) showAccountServers(agenda, servers, session)
  // The badge counts what waits: read on opening and each time the app comes back to the front.
  void approvals.refresh()
  refreshWhenServersChange(servers, approvals)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") void approvals.refresh()
  })
}

// The worker is a plain build output: `npm run dev` does not serve /sw.js, so it is registered only in a build.
if (!import.meta.env.DEV && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => console.warn("service worker: registration failed"))
}

void start()
