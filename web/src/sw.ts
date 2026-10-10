// Service worker: keeps the app shell for offline starts. It is built on its own, as a classic script (vite.sw.config.ts).
// It never touches a server, the Cloud or /config.json: those are not its requests, so APIs are never cached.
declare const self: ServiceWorkerGlobalScope
// Replaced at build time by vite.sw.config.ts: the bundle's files and the cache name for this build.
declare const __WC_ASSETS: string[] | undefined
declare const __WC_CACHE: string | undefined

const PREFIX = "wristcall-shell-"
const CACHE = typeof __WC_CACHE === "string" ? __WC_CACHE : `${PREFIX}dev`
const ASSETS = typeof __WC_ASSETS !== "undefined" ? __WC_ASSETS : []
const SHELL = ["/", "/index.html", "/manifest.webmanifest", ...ASSETS.map((file) => `/${file}`)]

/** Whether the worker answers this request: GET navigations and /assets/ files of its own origin, never /config.json. */
export function handlesFetch(request: Pick<Request, "url" | "method" | "mode">): boolean {
  if (request.method !== "GET") return false
  const url = new URL(request.url)
  if (url.origin !== self.location.origin || url.pathname === "/config.json") return false
  return request.mode === "navigate" || url.pathname.startsWith("/assets/")
}

async function respond(request: Request): Promise<Response> {
  const cache = await caches.open(CACHE)
  if (request.mode === "navigate") {
    // Network first. A navigation is never stored: /auth/callback?code=... must not end up in the Cache Storage.
    try {
      return await fetch(request)
    } catch (error) {
      const shell = await cache.match("/index.html")
      if (shell) return shell
      throw error
    }
  }
  // Cache first, and only what install stored: a miss goes to the network and stays out of the cache.
  return (await cache.match(request)) ?? fetch(request)
}

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL.map((url) => new Request(url, { cache: "reload" })))))
})

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((names) => Promise.all(names.filter((name) => name.startsWith(PREFIX) && name !== CACHE).map((name) => caches.delete(name))))
      .then(() => self.clients.claim()),
  )
})

self.addEventListener("fetch", (event) => {
  if (handlesFetch(event.request)) event.respondWith(respond(event.request))
})
