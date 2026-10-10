import { render } from "preact"
import "./ui/styles.css"
import { loadConfig } from "./config"
import { ServersModel } from "./models/servers"
import { openDb } from "./store/db"
import { ServerStore } from "./store/servers"
import { App } from "./ui/App"
import { BaseModel } from "./ui/useModel"

// Pending approvals arrive with the approvals model; until then the badge stays empty.
class NoBadge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

async function start(): Promise<void> {
  const config = await loadConfig()
  const servers = new ServersModel(new ServerStore(await openDb()))
  render(<App badge={new NoBadge()} config={config} servers={servers} />, document.getElementById("app")!)
}

// The worker is a plain build output: `npm run dev` does not serve /sw.js, so it is registered only in a build.
if (!import.meta.env.DEV && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => console.warn("service worker: registration failed"))
}

void start()
