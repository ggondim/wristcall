import { execFileSync } from "node:child_process"
import { expect, type Page, type Response } from "@playwright/test"
import { zitadelPassword, type E2eEnv } from "./env"
import { signInAtZitadel } from "./zitadel"

export const ORIGIN = "http://localhost:5173"

/** `POST {cloud}/v1/devices` answered `201`: the email sign-in ending in a Cloud `web` device. */
function isDeviceCreated(cloud: string): (response: Response) => boolean {
  return (response) => response.url() === `${cloud}/v1/devices` && response.request().method() === "POST" && response.status() === 201
}

/**
 * Signs in with email: the sign-in screen's "Sign in with email" button, the test user at Zitadel, and the exchange of
 * that login for a Cloud `web` device (`POST /v1/devices`, `201`). Returns the account access token of that response
 * (never Zitadel's: the Cloud takes no Zitadel token on its account routes). The token lives only in the page's memory
 * and the specs' variables; they never print, attach or assert on it.
 */
export async function signIn(page: Page, env: E2eEnv): Promise<string> {
  const created = page.waitForResponse(isDeviceCreated(env.cloud), { timeout: 120_000 })
  await page.goto("/sign-in")
  await page.getByRole("button", { name: "Sign in with email" }).click()
  // The page leaves the site for Zitadel first; the helper returns once it is back. A Zitadel session that is still
  // open (the app's sign-out does not end it) may send the browser straight back: then the device is made at once.
  const left = page.waitForURL((url) => url.origin !== ORIGIN, { timeout: 30_000 }).then(
    () => true,
    () => false,
  )
  if (await Promise.race([left, created.then(() => false)])) {
    await signInAtZitadel(page, { user: env.zitadelUser, password: zitadelPassword(env), appOrigin: ORIGIN })
  }
  const body = (await (await created).json()) as { access_token?: unknown }
  expect(typeof body.access_token, "the Cloud answers the device with an access token").toBe("string")
  await expect(page).toHaveURL(`${ORIGIN}/agents`)
  await page.getByRole("link", { name: "Settings" }).click()
  await expect(page.getByText("Signed in", { exact: true })).toBeVisible()
  return body.access_token as string
}

/** Which account keys the page's IndexedDB (`wristcall`, store `kv`) holds: values are never read out. */
export async function storedAccountKeys(page: Page): Promise<{ device: boolean; refresh: boolean; idToken: boolean }> {
  return page.evaluate(
    () =>
      new Promise<{ device: boolean; refresh: boolean; idToken: boolean }>((resolve, reject) => {
        const open = indexedDB.open("wristcall")
        open.onerror = () => reject(new Error("could not open IndexedDB"))
        open.onsuccess = () => {
          const db = open.result
          const store = db.transaction("kv", "readonly").objectStore("kv")
          const keys = store.getAllKeys()
          keys.onerror = () => reject(new Error("could not list keys"))
          keys.onsuccess = () => {
            db.close()
            const names = keys.result.map(String)
            resolve({ device: names.includes("account.device"), refresh: names.includes("account.refresh"), idToken: names.includes("account.id_token") })
          }
        }
      }),
  )
}

export function bearer(token: string): Record<string, string> {
  return { Authorization: `Bearer ${token}` }
}

/** A pairing code for the local user, made on the server's host. */
export function newPairingCode(env: E2eEnv): string {
  const out = execFileSync(env.cli, ["pair", "--user", env.user], {
    env: { ...process.env, WRISTCALL_CONFIG: env.config },
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  })
  const match = /^Pairing code:\s*([\d ]+)$/m.exec(out)
  if (match === null) throw new Error("the pair command printed no code")
  return (match[1] ?? "").replace(/\s+/g, "")
}
