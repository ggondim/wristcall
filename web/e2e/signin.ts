import { execFileSync } from "node:child_process"
import { expect, type Page, type Response } from "@playwright/test"
import { zitadelPassword, type E2eEnv } from "./env"
import { signInAtZitadel } from "./zitadel"

export const ORIGIN = "http://localhost:5173"

function isTokenExchange(response: Response): boolean {
  return response.url().endsWith("/oauth/v2/token") && response.request().method() === "POST"
}

/**
 * Clicks "Sign in" in Settings, plays the human at Zitadel and returns the access token of the new session. The token
 * lives only in the page's memory; the specs take it from the response of the code exchange and never print, attach or
 * assert on it.
 */
export async function signIn(page: Page, env: E2eEnv): Promise<string> {
  const exchange = page.waitForResponse(isTokenExchange, { timeout: 120_000 })
  await page.goto("/settings")
  await page.getByRole("button", { name: "Sign in" }).click()
  // The page leaves the site for Zitadel first; the helper returns once it is back.
  await page.waitForURL((url) => url.origin !== ORIGIN, { timeout: 30_000 })
  await signInAtZitadel(page, { user: env.zitadelUser, password: zitadelPassword(env), appOrigin: ORIGIN })
  const body = (await (await exchange).json()) as { access_token?: unknown }
  expect(typeof body.access_token, "the token endpoint answers with an access token").toBe("string")
  await expect(page).toHaveURL(`${ORIGIN}/settings`)
  await expect(page.getByText("Signed in", { exact: true })).toBeVisible()
  return body.access_token as string
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
