import { render } from "preact"
import "./ui/styles.css"
import { loadConfig } from "./config"
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
  render(<App badge={new NoBadge()} config={config} />, document.getElementById("app")!)
}

// The worker is a plain build output: `npm run dev` does not serve /sw.js, so it is registered only in a build.
if (!import.meta.env.DEV && "serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => console.warn("service worker: registration failed"))
}

void start()
