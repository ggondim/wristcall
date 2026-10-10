import { render } from "preact"
import "./ui/styles.css"
import { loadConfig } from "./config"
import { ApprovalsBadge, ApprovalsModel, refreshWhenServersChange } from "./models/approvals"
import { openServerStorage, ServersModel } from "./models/servers"
import { App } from "./ui/App"

async function start(): Promise<void> {
  const config = await loadConfig()
  const servers = new ServersModel(await openServerStorage())
  const approvals = new ApprovalsModel(servers)
  render(
    <App approvals={approvals} badge={new ApprovalsBadge(approvals)} config={config} servers={servers} />,
    document.getElementById("app")!,
  )
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
