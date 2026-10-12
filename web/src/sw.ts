// Service worker: keeps the app shell for offline starts. It is built on its own, as a classic script (vite.sw.config.ts).
// It never touches a server, the Cloud or /config.json: those are not its requests, so APIs are never cached.
import { clickTarget, notificationFor, parsePush, type WorkerMessage } from "./push/payload"

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


async function tellWindows(message: WorkerMessage): Promise<void> {
  try {
    for (const client of await self.clients.matchAll({ type: "window", includeUncontrolled: true })) client.postMessage(message)
  } catch {
    // No window to tell, or none that can be.
  }
}

function readText(data: PushMessageData | null | undefined): string | null {
  try {
    return data?.text() ?? null
  } catch {
    return null
  }
}

// Every push shows a notification (a browser takes a push that shows nothing as abuse): one that cannot be read
// is the generic "wristcall". The windows hear of it too, so the badge does not wait for its next check.
self.addEventListener("push", (event) => {
  const { title, options } = notificationFor(parsePush(readText(event.data)))
  const shown = Promise.resolve(self.registration.showNotification(title, options)).catch(() => {
    // Odd options (an icon, say) must not leave the push unshown.
    const generic = notificationFor(null)
    return self.registration.showNotification(generic.title, generic.options)
  })
  event.waitUntil(Promise.all([shown, tellWindows({ kind: "approvals" })]))
})

function sameOrigin(client: { url?: string }): boolean {
  try {
    return new URL(client.url ?? "", self.location.origin).origin === self.location.origin
  } catch {
    return false
  }
}

async function openApp(target: string): Promise<void> {
  let windows: readonly WindowClient[] = []
  try {
    windows = (await self.clients.matchAll({ type: "window", includeUncontrolled: true })).filter(sameOrigin)
  } catch {
    // Falls through to a new window.
  }
  const win = windows.find((client) => client.focused) ?? windows.find((client) => client.visibilityState === "visible") ?? windows[0]
  if (win !== undefined) {
    // The window goes to the target by itself (no reload): `client.navigate` only works for a window this worker controls.
    try {
      await win.focus()
    } catch {
      // Focus can be refused; the message still moves the app.
    }
    win.postMessage({ kind: "open", target } satisfies WorkerMessage)
    return
  }
  await self.clients.openWindow(target)
}

// A tap opens the approvals screen, at the request when the notification names one. There are no action buttons:
// approving is done on the screen, after the app unlocks.
self.addEventListener("notificationclick", (event) => {
  event.notification.close()
  event.waitUntil(openApp(clickTarget(event.notification.data)).catch(() => {}))
})
