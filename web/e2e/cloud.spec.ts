import { readFileSync } from "node:fs"
import { join } from "node:path"
import { expect, test, type APIRequestContext, type Page } from "@playwright/test"
import { ORIGIN } from "./signin"

// The Wristcall Cloud entry end to end: a local Cloud with the entry turned on for `web/0.4.0`, a cloud-mode server
// registered as its node, and an account with the `full` plan whose iPhone (`ios` device session) approves this browser's
// QR sign-in through the Cloud's API, as the iPhone app does after scanning the code. The private docs repo's
// `execucao/o3e/e2e-local.sh` sets all of it up and runs this spec once per phase:
//
// - `no-node`: the node is down, so the route answers `503 no_node_available`;
// - `node`: the node is up (a "Voice notes" call saved to the history when WRISTCALL_E2E_CLOUD_SAVED_CALL is `1`);
// - `usage`: the month's usage was set to 480 of 600 minutes, so the 80 % warning shows.
//
// WRISTCALL_E2E_CLOUD_ACCOUNT_FILE is a JSON file holding `{"ios": {"access_token": ...}}`; the token is never printed.
// Screenshots go to WRISTCALL_E2E_SHOTS when it is set.

const CLOUD = (process.env.WRISTCALL_E2E_CLOUD ?? "").trim().replace(/\/+$/, "")
const PHASE = (process.env.WRISTCALL_E2E_CLOUD_PHASE ?? "").trim()
const ACCOUNT_FILE = (process.env.WRISTCALL_E2E_CLOUD_ACCOUNT_FILE ?? "").trim()
const SHOTS = (process.env.WRISTCALL_E2E_SHOTS ?? "").trim()
const SAVED_CALL = process.env.WRISTCALL_E2E_CLOUD_SAVED_CALL === "1"

const WEB_CLIENT = "web/0.4.0"
const ENTRY = "Wristcall Cloud"
const NO_NODE = "Wristcall Cloud is temporarily unavailable. Try again in a minute."

function skipReason(): string | null {
  const missing = [
    ["WRISTCALL_E2E_CLOUD", CLOUD],
    ["WRISTCALL_E2E_CLOUD_PHASE", PHASE],
    ["WRISTCALL_E2E_CLOUD_ACCOUNT_FILE", ACCOUNT_FILE],
  ].filter(([, v]) => v === "")
  return missing.length === 0 ? null : `${missing.map(([n]) => n).join(", ")} not set`
}

/** The account's iPhone session: only its access token, read when needed. */
function iPhoneToken(): string {
  const token = (JSON.parse(readFileSync(ACCOUNT_FILE, "utf8")) as { ios?: { access_token?: unknown } }).ios?.access_token
  if (typeof token !== "string" || token === "") throw new Error("the account file has no ios access token")
  return token
}

/** The whole page in one viewport: a full-page capture would paint the fixed tab bar over the middle of a long page. */
async function shot(page: Page, name: string): Promise<void> {
  if (SHOTS === "") return
  const size = page.viewportSize() ?? { width: 1280, height: 860 }
  const height = await page.evaluate(() => document.documentElement.scrollHeight)
  if (height > size.height) await page.setViewportSize({ width: size.width, height })
  await page.screenshot({ path: join(SHOTS, name) })
  if (height > size.height) await page.setViewportSize(size)
}

/**
 * Opens the sign-in screen, takes the web session the page created (`POST /v1/web-sessions`, the QR's id and approve
 * key) and approves it with the iPhone's session, then waits for the page to sign in on its own (it polls).
 */
async function signInWithIPhone(page: Page, request: APIRequestContext): Promise<void> {
  const created = page.waitForResponse(
    (r) => r.url() === `${CLOUD}/v1/web-sessions` && r.request().method() === "POST" && r.status() === 201,
  )
  await page.goto("/sign-in")
  const session = (await (await created).json()) as { id: string; approve_key: string }
  await expect(page.getByRole("status").filter({ hasText: "Waiting for your iPhone…" })).toBeVisible()
  const approved = await request.post(`${CLOUD}/v1/web-sessions/${encodeURIComponent(session.id)}/approve`, {
    headers: { Authorization: `Bearer ${iPhoneToken()}`, "X-Wristcall-Client": "ios/0.3.0" },
    data: { approve_key: session.approve_key },
  })
  expect(approved.status(), "the iPhone approves the web session").toBe(200)
  await expect(page).toHaveURL(`${ORIGIN}/agents`, { timeout: 30_000 })
}

/** Every request to the Cloud carries the PWA's client header; none to anything else (the node, own servers) does. */
function watchHeaders(page: Page): { cloud: string[]; others: string[] } {
  const seen = { cloud: [] as string[], others: [] as string[] }
  page.on("request", (r) => {
    const url = r.url()
    if (url.startsWith(ORIGIN)) return
    const header = r.headers()["x-wristcall-client"] ?? "(none)"
    if (url.startsWith(`${CLOUD}/`)) seen.cloud.push(header)
    else seen.others.push(header)
  })
  return seen
}

function cloudSection(page: Page) {
  return page.getByRole("region", { name: ENTRY })
}

/** Signs this browser out (Settings › Sign out), which also removes its `web` device from the account. */
async function signOut(page: Page): Promise<void> {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Settings" }).click()
  await page.getByRole("button", { name: "Sign out" }).click()
  await expect(page.getByRole("heading", { name: "Sign in" }).or(page.getByRole("link", { name: "Sign in" })).first()).toBeVisible()
}

async function openSettingsPlan(page: Page) {
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Settings" }).click()
  const plan = page.getByRole("list", { name: "Plan" })
  await expect(plan).toContainText(ENTRY)
  await expect(plan).toContainText("Beta · invite")
  return plan
}

const reason = skipReason()

test.describe("Wristcall Cloud entry", () => {
  test.skip(reason !== null, reason ?? "")
  test.use({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 2 })

  test("without a node: the entry and its agents, unavailable", async ({ page, request }) => {
    test.skip(PHASE !== "no-node", `phase ${PHASE}`)
    const headers = watchHeaders(page)
    await signInWithIPhone(page, request)

    const section = cloudSection(page)
    await expect(section.getByRole("alert")).toHaveText(NO_NODE)
    // The agenda's copy of the agents, read only while the node cannot be reached.
    await expect(section.getByRole("list", { name: `${ENTRY} agents` })).toContainText("Voice notes")
    await expect(section.getByRole("link", { name: /Voice notes/ })).toHaveCount(0)
    await shot(page, "o3e-web-01-agents-no-node.png")

    const plan = await openSettingsPlan(page)
    await expect(plan).toContainText("Usage unavailable right now.")

    await signOut(page)
    expect(headers.cloud.length).toBeGreaterThan(0)
    expect(new Set(headers.cloud)).toEqual(new Set([WEB_CLIENT]))
    expect(headers.others.filter((h) => h !== "(none)")).toEqual([])
  })

  test("with a node: agents, plan usage, saved call and the Voice notes editor", async ({ page, request }) => {
    test.skip(PHASE !== "node", `phase ${PHASE}`)
    const headers = watchHeaders(page)
    await signInWithIPhone(page, request)

    const section = cloudSection(page)
    const voiceNotes = section.getByRole("link", { name: /Voice notes/ })
    await expect(voiceNotes).toBeVisible()
    await expect(voiceNotes).toContainText("One-shot")
    await expect(section.getByRole("alert")).toHaveCount(0)
    await expect(page.locator(".usage-banner")).toHaveCount(0)
    await shot(page, "o3e-web-01b-agents.png")

    const plan = await openSettingsPlan(page)
    await expect(plan).toContainText(/\d+ of 600 min used this month/)

    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "History" }).click()
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible()
    if (SAVED_CALL) {
      const calls = page.getByRole("list", { name: "Calls" })
      const saved = calls.getByRole("link").filter({ hasText: "Voice notes" }).first()
      await expect(saved).toContainText("Saved")
      await shot(page, "o3e-web-03-history-saved.png")
      await saved.click()
      await expect(page.getByText("Saved to History", { exact: true })).toBeVisible()
    }

    await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: "Agents" }).click()
    await cloudSection(page).getByRole("link", { name: /Voice notes/ }).click()
    await expect(page.getByRole("heading", { name: "Edit agent" })).toBeVisible()
    await expect(page.locator("select[name=action] option:checked")).toHaveText("None, save to History")
    await shot(page, "o3e-web-04-editor.png")

    await signOut(page)
    expect(new Set(headers.cloud)).toEqual(new Set([WEB_CLIENT]))
    expect(headers.others.length, "the PWA reached the node").toBeGreaterThan(0)
    expect(headers.others.filter((h) => h !== "(none)")).toEqual([])
  })

  test("at 80 % of the month's minutes: the warning and the plan's usage", async ({ page, request }) => {
    test.skip(PHASE !== "usage", `phase ${PHASE}`)
    await signInWithIPhone(page, request)

    await expect(page.locator(".usage-banner")).toHaveText("You've used 80% of this month's call minutes.")
    await shot(page, "o3e-web-02b-agents-usage-warning.png")

    const plan = await openSettingsPlan(page)
    await expect(plan).toContainText("480 of 600 min used this month")
    await expect(page.getByRole("list", { name: "Devices" }).getByText("This browser")).toBeVisible()
    await shot(page, "o3e-web-02-plan-usage.png")
    await signOut(page)
  })
})
