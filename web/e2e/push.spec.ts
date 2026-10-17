import { createECDH, randomBytes } from "node:crypto"
import { readFileSync, statSync } from "node:fs"
import { expect, test } from "@playwright/test"
import { e2eEnv, pushSkipReason } from "./env"
import { bearer, newPairingCode, ORIGIN, signIn } from "./signin"

// Device approval notifications, end to end: sign in, add the local server with a pairing code, turn notifications on (a fixed Web Push
// subscription stands in for the browser's push service, which headless Chromium lacks), register at the Cloud's
// relay and at the server, make a watch ask to sign in (the Cloud's fake channel logs the send), hand a push message
// of the relay's shape to the service worker through the DevTools protocol, check the notification and open the
// approvals screen it points at.
//
// The access token lives only in the page's memory and is never printed. The relay answers the registration with a
// push key, which the spec never reads.
const env = e2eEnv()
const skip = pushSkipReason(env)

test.skip(skip !== null, skip ?? "")

const ENDPOINT = "https://fcm.googleapis.com/fcm/send/e2e"
const DEVICE_NAME = "E2E push watch"
const SENT = "fake push accepted (platform webpush, event device.approval)"

/** An uncompressed P-256 public key and a 16 byte auth secret, as a browser's subscription has them. */
function subscriptionKeys(): { p256dh: string; auth: string } {
  const ecdh = createECDH("prime256v1")
  ecdh.generateKeys()
  return { p256dh: ecdh.getPublicKey().toString("base64url"), auth: randomBytes(16).toString("base64url") }
}

function logSize(): number {
  return statSync(env.cloudLog).size
}

/** The Cloud's log from `offset` on: only what happened after a step. */
function logSince(offset: number): string {
  return readFileSync(env.cloudLog).subarray(offset).toString("utf8")
}

interface PendingRequest {
  request_id: string
  device_name: string
  expires_at: number
}

test("push: subscribe, relay send, notification, approvals link", async ({ page, context, request }) => {
  const keys = subscriptionKeys()
  await context.grantPermissions(["notifications"], { origin: ORIGIN })
  // The subscription exists after `subscribe` is called, and survives page loads (the page keeps nothing else).
  await context.addInitScript(
    ({ endpoint, p256dh, auth }) => {
      const FLAG = "wc-e2e-subscribed"
      const subscription = {
        endpoint,
        expirationTime: null,
        toJSON: () => ({ endpoint, expirationTime: null, keys: { p256dh, auth } }),
        unsubscribe: async () => {
          localStorage.removeItem(FLAG)
          return true
        },
      }
      const manager = PushManager.prototype as unknown as { subscribe: () => Promise<unknown>; getSubscription: () => Promise<unknown> }
      manager.subscribe = async () => {
        localStorage.setItem(FLAG, "1")
        return subscription
      }
      manager.getSubscription = async () => (localStorage.getItem(FLAG) === null ? null : subscription)
    },
    { endpoint: ENDPOINT, ...keys },
  )

  let token = ""
  let tag = ""
  let label = ""

  await test.step("sign in at Zitadel", async () => {
    token = await signIn(page, env)
  })

  await test.step("add the local server with a pairing code", async () => {
    await page.goto("/settings/servers/add")
    // Linked to the account with a pairing code: the server only takes account tokens from a linked account.
    await page.getByRole("button", { name: "Pairing code" }).click()
    await page.locator("input[name=address]").fill(env.server)
    await page.locator("input[name=code]").fill(newPairingCode(env))
    await page.locator("input[name=name]").fill("E2E push server")
    await page.getByRole("button", { name: "Add", exact: true }).click()
    await expect(page.getByRole("list", { name: "Servers" }).getByRole("link", { name: /E2E push server/ })).toBeVisible()
  })

  await test.step("turn notifications on", async () => {
    await page.goto("/settings")
    const turnOn = page.getByRole("button", { name: "Turn on notifications" })
    await expect(turnOn).toBeVisible({ timeout: 30_000 })
    const registered = page.waitForResponse((r) => r.url() === `${env.cloud}/v1/push/registrations` && r.request().method() === "POST")
    const keySet = page.waitForResponse((r) => r.url() === `${env.server}/v1/push` && r.request().method() === "PUT")
    await turnOn.click()
    const registration = await registered
    expect(registration.status(), "POST /v1/push/registrations").toBe(201)
    // What the relay was told (the tag and the label are not secret; the subscription and the push key are not read).
    const sent = registration.request().postDataJSON() as { tag: string; label: string; platform: string; events: string[]; subscription: { endpoint: string } }
    expect(sent.platform).toBe("webpush")
    expect(sent.events).toEqual(["device.approval"])
    expect(sent.subscription.endpoint).toBe(ENDPOINT)
    tag = sent.tag
    label = sent.label
    expect(tag, "the registration carries the server's local id as the tag").not.toBe("")
    expect((await keySet).status(), "PUT /v1/push").toBe(204)
    await expect(page.getByText("Notifications are on for this device.")).toBeVisible()
  })

  let requestId = ""
  let expiresAt = 0

  await test.step("a watch asks to sign in", async () => {
    const issued = await request.post(`${env.cloud}/v1/server-tokens`, { headers: bearer(token), data: { audience: env.server } })
    expect(issued.status(), "POST /v1/server-tokens").toBe(200)
    const serverToken = ((await issued.json()) as { token: string }).token
    const before = logSize()
    const pair = await request.post(`${env.server}/v1/pair/account`, { data: { token: serverToken, device_name: DEVICE_NAME } })
    expect(pair.status(), "POST /v1/pair/account").toBe(202)
    // The server tells the relay, the relay hands the send to its (fake) Web Push channel: the log says so, after the 202.
    await expect.poll(() => logSince(before).includes(SENT), { message: "the Cloud log shows the fake push send", timeout: 30_000 }).toBe(true)
    const list = await request.get(`${env.server}/v1/pairing-requests`, { headers: bearer(env.pat) })
    expect(list.status(), "GET /v1/pairing-requests").toBe(200)
    const found = ((await list.json()) as { requests: PendingRequest[] }).requests.find((r) => r.device_name === DEVICE_NAME)
    expect(found, "the server lists the request").toBeDefined()
    requestId = found!.request_id
    expiresAt = found!.expires_at
    expect(requestId).toMatch(/^[0-9]{4}$/)
  })

  await test.step("the service worker shows the notification", async () => {
    // The worker must be active before a push can reach it.
    await page.evaluate(() => navigator.serviceWorker.ready.then(() => true))
    const cdp = await context.newCDPSession(page)
    let registrationId = ""
    cdp.on("ServiceWorker.workerRegistrationUpdated", (event) => {
      for (const registration of event.registrations) if (registration.scopeURL.startsWith(`${ORIGIN}/`)) registrationId = registration.registrationId
    })
    await cdp.send("ServiceWorker.enable")
    await expect.poll(() => registrationId, { message: "the service worker registration of the site", timeout: 15_000 }).not.toBe("")
    // A message of the relay's shape (docs/protocol.md, "Push relay"), as the push service would hand it over.
    const message = {
      v: 1,
      event: "device.approval",
      tag,
      title: "New device",
      subtitle: label,
      body: `${DEVICE_NAME} wants to sign in`,
      data: { request_id: requestId, device_name: DEVICE_NAME, expires_at: expiresAt },
    }
    const deliver = () => cdp.send("ServiceWorker.deliverPushMessage", { origin: ORIGIN, registrationId, data: JSON.stringify(message) })
    const shown = async () =>
      page.evaluate(async () => {
        const registration = await navigator.serviceWorker.ready
        return (await registration.getNotifications()).map((n) => ({ title: n.title, body: n.body, tag: n.tag, data: n.data as unknown }))
      })
    // Delivery by the protocol is not acknowledged: it is repeated until the notification shows (the notification has a
    // tag per request, so a second delivery replaces the first and never adds one).
    await expect
      .poll(
        async () => {
          await deliver()
          return (await shown()).map((n) => n.title)
        },
        { message: "notifications on the registration", timeout: 20_000, intervals: [1_000] },
      )
      .toContain("New device")
    const notification = (await shown()).find((n) => n.title === "New device")!
    expect(notification.body).toContain(DEVICE_NAME)
    expect(notification.tag).toBe(`approval:${tag}:${requestId}`)
    // The server's expiry is fractional seconds; the app keeps whole seconds.
    expect(notification.data).toEqual({ tag, requestId, expiresAt: Math.floor(expiresAt) })
    await cdp.detach()
  })

  await test.step("the link of the notification highlights the request", async () => {
    // What a tap opens: the approvals screen of that server and request (no buttons on the notification).
    await page.goto(`/approvals?server=${encodeURIComponent(tag)}&request=${requestId}&expires=${Math.floor(expiresAt)}`)
    const row = page.locator("li.approval.focused")
    await expect(row).toBeVisible()
    await expect(row).toHaveAttribute("aria-current", "true")
    await expect(row).toContainText(DEVICE_NAME)
    // The service worker only shows and opens: deny the request here, on screen, as a person would.
    const denied = page.waitForResponse((r) => /\/v1\/pairing-requests\/[^/]+\/deny$/.test(new URL(r.url()).pathname) && r.request().method() === "POST")
    await row.getByRole("button", { name: "Deny", exact: true }).click()
    expect((await denied).status(), "deny").toBe(204)
    await expect(page.getByRole("heading", { name: "Nothing to approve" })).toBeVisible()
    await page.evaluate(async () => {
      const registration = await navigator.serviceWorker.ready
      for (const n of await registration.getNotifications()) n.close()
    })
  })

  await test.step("removing the server takes its push key along", async () => {
    await page.goto("/settings/servers")
    await page.getByRole("link", { name: /E2E push server/ }).click()
    const cleared = page.waitForResponse((r) => r.url() === `${env.server}/v1/push` && r.request().method() === "DELETE")
    const forgotten = page.waitForResponse((r) => r.url() === `${env.cloud}/v1/push/registrations/current` && r.request().method() === "DELETE")
    await page.getByRole("button", { name: "Remove server" }).click()
    await page.getByRole("region", { name: "Remove server" }).getByRole("button", { name: "Remove" }).click()
    expect((await cleared).status(), "DELETE /v1/push").toBe(204)
    // The server's own clean-up may have removed the registration at the relay first: then the app's call finds nothing.
    expect([204, 404], "DELETE /v1/push/registrations/current").toContain((await forgotten).status())
    await expect(page.getByRole("heading", { name: "No servers yet." })).toBeVisible()
  })
})
