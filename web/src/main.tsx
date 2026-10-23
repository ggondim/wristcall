import { render } from "preact"
import "./ui/styles.css"
import { AgendaSync } from "./account/agenda"
import { CloudRouter } from "./account/router"
import { AccountSession } from "./account/session"
import { CloudClient } from "./api/cloud"
import { RelayClient } from "./api/relay"
import { loadConfig } from "./config"
import { AccountModel } from "./models/account"
import { SignInModel } from "./models/signIn"
import { ApprovalsBadge, ApprovalsModel, refreshWhenServersChange } from "./models/approvals"
import { defaultServerClient, openServerStorage, ServersModel } from "./models/servers"
import { parseWorkerMessage } from "./push/payload"
import { NotificationsModel } from "./push/notifications"
import { PushSync } from "./push/sync"
import { persistOnFirstServer, persistWhenServersExist } from "./store/persist"
import { ServerStore } from "./store/servers"
import { App } from "./ui/App"
import { navigate } from "./ui/route"

/**
 * The agenda's servers this browser has no token for ("From your account") and the Wristcall Cloud entry, read now and
 * when the account or the page changes. An agenda that cannot be read keeps the Cloud entry shown.
 */
function showAccountServers(agenda: AgendaSync, servers: ServersModel, session: AccountSession): void {
  const refresh = (): void => {
    void agenda.read().then(({ pending, cloud }) => {
      servers.setFromAccount(pending)
      if (cloud !== undefined) servers.setCloud(cloud)
    })
  }
  refresh()
  session.onStateChange(refresh)
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh()
  })
}

/** Web Push needs the worker, which only a build registers (`npm run dev` does not serve /sw.js). */
const hasWorker = !import.meta.env.DEV && "serviceWorker" in navigator

/** What the worker tells the page: open the screen a tapped notification points at, or look at the approvals again. */
function listenToWorker(approvals: ApprovalsModel): void {
  if (!hasWorker) return
  navigator.serviceWorker.addEventListener("message", (event: MessageEvent) => {
    const message = parseWorkerMessage(event.data)
    if (message?.kind === "open") navigate(message.target)
    else if (message?.kind === "approvals") void approvals.refresh()
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
    // The node of the Cloud entry: the session drops its route when the sign-in changes.
    const router = new CloudRouter(session)
    session.attachRouter(router)
    servers.attachRouter(router)
    await session.load().catch(() => {}) // unreadable: signed out
  }
  // The agenda mirror: every change of the servers (and of an agents list) is queued for the account's agenda.
  const agenda = session === null || !(storage instanceof ServerStore) ? null : new AgendaSync(session, servers, storage)
  if (agenda !== null) servers.onChange((event) => agenda.serverChanged(event))
  persistOnFirstServer(servers)
  void persistWhenServersExist(storage) // servers saved in an earlier session, and storage still not persistent
  // Push: the Cloud is the relay (never one a server names). A server leaving takes its key along, and so does
  // deleting the account.
  const push =
    cloud === null || session === null || !(storage instanceof ServerStore)
      ? null
      : new PushSync(storage, new RelayClient(cloud.url), defaultServerClient, cloud.url)
  // The server already deleted its key (`ServersModel.remove`); what is left is the relay's side.
  if (push !== null) servers.setBeforeRemove((server) => push.forget(server, { serverCleared: true }))
  const accountSession = session
  const notifications =
    push === null || accountSession === null || !hasWorker
      ? null
      : new NotificationsModel({
          config: async () => {
            const cloudConfig = await accountSession.config()
            return { webpush: cloudConfig.push.webpush, vapid: cloudConfig.push.vapid_public_key }
          },
          sync: push,
        })
  const account = session === null ? null : new AccountModel(session, agenda, push, { cloud, servers })
  const signIn = cloud === null || account === null ? null : new SignInModel({ cloud, account })
  render(
    <App
      account={account}
      signIn={signIn}
      notifications={notifications}
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
  listenToWorker(approvals)
  // The push keys are checked on opening, on each return to the front and when a server is added or changed.
  if (notifications !== null) {
    void notifications.refresh()
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") void notifications.refresh()
    })
    servers.onChange((event) => {
      if (event.kind !== "removed") void notifications.refresh()
    })
  }
}

// The worker is a plain build output: `npm run dev` does not serve /sw.js, so it is registered only in a build.
if (!import.meta.env.DEV && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => console.warn("service worker: registration failed"))
}

void start()
