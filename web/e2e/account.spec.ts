import { expect, test, type APIRequestContext } from "@playwright/test"
import { accountSkipReason, e2eEnv } from "./env"
import { bearer, newPairingCode, ORIGIN, signIn as signInTo } from "./signin"

// The account flows against the local Cloud and server and the real Zitadel (the test user of the E7 setup):
// sign in, add the server by pairing code, check the agenda, approve a watch sign-in request, sign out, sign in again
// and delete the account (the Cloud here has no Zitadel machine credentials: it deletes the agenda only, the test
// user stays). Signing in is in `signin.ts` (the access token stays in memory and is never printed).
const env = e2eEnv()
const skip = accountSkipReason(env)

test.skip(skip !== null, skip ?? "")

/** Prints which audiences and client the PWA's access token carries (claims only, never the token). */
function describeToken(token: string): void {
  try {
    const claims = JSON.parse(Buffer.from(token.split(".")[1] ?? "", "base64url").toString("utf8")) as Record<string, unknown>
    const show = (value: unknown): string => (Array.isArray(value) ? value.join(",") : String(value))
    console.log(`[account-e2e] access token claims: aud=${show(claims.aud)} client_id=${show(claims.client_id ?? claims.azp)} iss=${show(claims.iss)}`)
  } catch {
    console.log("[account-e2e] access token is not a JWT")
  }
}

async function agendaUrls(request: APIRequestContext, token: string): Promise<string[]> {
  const response = await request.get(`${env.cloud}/v1/servers`, { headers: bearer(token) })
  expect(response.status(), "GET /v1/servers").toBe(200)
  return ((await response.json()) as { servers: { url: string }[] }).servers.map((server) => server.url)
}

test("account: sign in, link, agenda, approval, sign out, delete", async ({ page, request }) => {
  const serverHost = new URL(env.server).host
  let token = ""

  await test.step("sign in at Zitadel", async () => {
    token = await signInTo(page, env)
    describeToken(token)
  })

  await test.step("add the server with a pairing code", async () => {
    await page.getByRole("link", { name: "Settings" }).click()
    await page.getByRole("link", { name: /^Servers/ }).click()
    await page.getByRole("link", { name: "Add server" }).click()
    await page.getByRole("button", { name: "Pairing code" }).click()
    await page.locator("input[name=address]").fill(env.server)
    await page.locator("input[name=code]").fill(newPairingCode(env))
    await page.locator("input[name=name]").fill("E2E linked server")
    await page.getByRole("button", { name: "Add", exact: true }).click()
    const row = page.getByRole("list", { name: "Servers" }).getByRole("link", { name: /E2E linked server/ })
    await expect(row).toBeVisible()
    await expect(row.getByText("Account", { exact: true })).toBeVisible()
    await row.click()
    await expect(page.getByText("Linked to wristcall account")).toBeVisible()
  })

  await test.step("the agenda has the server", async () => {
    await expect
      .poll(async () => (await agendaUrls(request, token)).some((url) => new URL(url).host === serverHost), {
        message: "the Cloud agenda lists the server",
        timeout: 30_000,
      })
      .toBe(true)
  })

  await test.step("approve a watch sign-in request", async () => {
    // The spec plays the watch: a per-server token from the Cloud, then the request to the server.
    const issued = await request.post(`${env.cloud}/v1/server-tokens`, { headers: bearer(token), data: { audience: env.server } })
    expect(issued.status(), "POST /v1/server-tokens").toBe(200)
    const serverToken = ((await issued.json()) as { token: string }).token
    const pair = await request.post(`${env.server}/v1/pair/account`, { data: { token: serverToken, device_name: "E2E watch" } })
    expect(pair.status(), "POST /v1/pair/account").toBe(202)
    const pollToken = ((await pair.json()) as { poll_token: string }).poll_token

    // Open the screen as a relaunch would: the session comes back from the stored refresh token.
    await page.goto("/approvals")
    const waiting = page.getByRole("list", { name: "Waiting requests" }).getByRole("listitem").filter({ hasText: "E2E watch" })
    await expect(waiting).toBeVisible()
    await waiting.getByRole("button", { name: "Approve", exact: true }).click()
    // Approving hands a credential to the watch: the app asks first.
    await expect(waiting.getByText(/Approve E2E watch\?/)).toBeVisible()
    const approved = page.waitForResponse((r) => /\/v1\/pairing-requests\/[^/]+\/approve$/.test(new URL(r.url()).pathname) && r.request().method() === "POST")
    await waiting.getByRole("button", { name: "Approve E2E watch" }).click()
    expect((await approved).status(), "approve").toBe(200)
    await expect(page.getByRole("heading", { name: "Nothing to approve" })).toBeVisible()

    // The watch's next poll gets its credential.
    const poll = await request.post(`${env.server}/v1/pair/poll`, { data: { poll_token: pollToken } })
    expect(poll.status(), "POST /v1/pair/poll").toBe(200)
  })

  await test.step("sign out", async () => {
    await page.goto("/settings")
    await expect(page.getByText("Signed in", { exact: true })).toBeVisible()
    // The sign-out must reach the provider: the page asks Zitadel's end_session endpoint.
    const endSession = page.waitForRequest((r) => new URL(r.url()).pathname === "/oidc/v1/end_session", { timeout: 60_000 })
    await page.getByRole("button", { name: "Sign out" }).click()
    expect((await endSession).method(), "the end_session request fired").toBe("GET")
    // Then back to the site's start page.
    await page.waitForURL(`${ORIGIN}/`, { timeout: 60_000 })
    await page.goto("/settings")
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible()
  })

  await test.step("sign in again and delete the account", async () => {
    token = await signInTo(page, env)
    await page.getByRole("button", { name: "Delete account" }).click()
    const confirm = page.getByRole("region", { name: "Delete account" })
    await expect(confirm).toBeVisible()
    const deleted = page.waitForResponse((r) => new URL(r.url()).pathname === "/v1/account" && r.request().method() === "DELETE")
    await confirm.getByRole("button", { name: "Delete account" }).click()
    expect((await deleted).status(), "DELETE /v1/account").toBe(204)
    // Deleting signs out, which ends the session at Zitadel and comes back to the start page.
    await page.waitForURL(`${ORIGIN}/`, { timeout: 60_000 })
    await page.goto("/settings")
    await expect(page.getByRole("button", { name: "Sign in" })).toBeVisible()
    // The agenda is gone with the account: the old token still names it, and the Cloud answers with an empty one.
    const after = await request.get(`${env.cloud}/v1/servers`, { headers: bearer(token) })
    expect([200, 401, 404], "GET /v1/servers after the deletion").toContain(after.status())
    if (after.status() === 200) expect(((await after.json()) as { servers: unknown[] }).servers).toEqual([])
  })
})
