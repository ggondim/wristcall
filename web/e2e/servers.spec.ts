import { expect, test } from "@playwright/test"
import { e2eEnv, serverSkipReason } from "./env"

// A site without a Cloud (empty cloudUrl) and a local server, reached with a personal token: add the server, make,
// edit and delete an agent, see a pairing code, open the history, remove the server.
const env = e2eEnv()
const skip = serverSkipReason(env)

test.skip(skip !== null, skip ?? "")

// The worker is blocked so that the route on config.json below keeps applying to every request.
test.use({ serviceWorkers: "block" })

test("manage a server with a personal token", async ({ page }) => {
  // The built site is served with the Cloud address of the other specs; this one runs as a site without accounts.
  await page.route("**/config.json", (route) => route.fulfill({ json: { cloudUrl: "" } }))

  await test.step("add the server", async () => {
    await page.goto("/")
    await expect(page.getByRole("heading", { name: "No agents yet." })).toBeVisible()
    await page.goto("/settings/servers")
    await expect(page.getByRole("heading", { name: "No servers yet." })).toBeVisible()
    await page.getByRole("link", { name: "Add server" }).click()
    await page.locator("input[name=address]").fill(env.server)
    await page.locator("input[name=token]").fill(env.pat)
    await page.locator("input[name=name]").fill("E2E server")
    await page.getByRole("button", { name: "Add", exact: true }).click()
    await expect(page.getByRole("heading", { name: "Servers" })).toBeVisible()
    const row = page.getByRole("list", { name: "Servers" }).getByRole("link", { name: /E2E server/ })
    await expect(row).toBeVisible()
    await expect(row).toContainText(/Version \d/)
  })

  await test.step("create an agent", async () => {
    await page.getByRole("link", { name: /E2E server/ }).click()
    await expect(page.getByRole("heading", { name: "E2E server" })).toBeVisible()
    // The tab bar has an "Agents" link too: take the one in the server detail's own list.
    await page.getByRole("list", { name: "Manage" }).getByRole("link", { name: "Agents" }).click()
    await expect(page.getByRole("heading", { name: "No agents" })).toBeVisible()
    await page.getByRole("link", { name: "New agent" }).click()
    await page.locator("input[name=name]").fill("E2E agent")
    await expect(page.locator("input[name=slug]")).toHaveValue("e2e-agent")
    await page.getByRole("button", { name: "Save" }).click()
    const agents = page.getByRole("list", { name: "Agents" })
    await expect(agents.getByText("E2E agent", { exact: true })).toBeVisible()
  })

  await test.step("edit the agent", async () => {
    await page.getByRole("list", { name: "Agents" }).getByRole("link", { name: /E2E agent/ }).click()
    await expect(page.getByRole("heading", { name: "Edit agent" })).toBeVisible()
    await page.locator("input[name=name]").fill("E2E agent renamed")
    await page.getByRole("button", { name: "Save" }).click()
    const agents = page.getByRole("list", { name: "Agents" })
    await expect(agents.getByText("E2E agent renamed", { exact: true })).toBeVisible()
  })

  await test.step("delete the agent", async () => {
    await page.getByRole("button", { name: "Delete E2E agent renamed" }).click()
    const confirm = page.getByRole("region", { name: "Delete agent" })
    await expect(confirm).toBeVisible()
    await confirm.getByRole("button", { name: "Delete" }).click()
    await expect(page.getByRole("heading", { name: "No agents" })).toBeVisible()
  })

  await test.step("show a pairing code", async () => {
    await page.getByRole("link", { name: "Server", exact: true }).click()
    await page.getByRole("link", { name: "Devices" }).click()
    await expect(page.getByRole("heading", { name: "Devices" })).toBeVisible()
    await page.getByRole("link", { name: "Pairing code" }).click()
    const code = page.locator("section[aria-label='Pairing code'] .code")
    await expect(code).toBeVisible()
    // The code is a secret for ten minutes: check its shape without putting it in a failure message.
    const shaped = /^\d{4} \d{4}$/.test((await code.innerText()).trim())
    expect(shaped, "the pairing code has 8 digits in two groups").toBe(true)
  })

  await test.step("open the history", async () => {
    await page.getByRole("link", { name: "History" }).click()
    await expect(page.getByRole("heading", { name: "History" })).toBeVisible()
    // Either the calls or the empty state, never a blank screen.
    await expect(page.getByRole("list", { name: "Calls" }).or(page.getByRole("heading", { name: "No history yet" }))).toBeVisible()
  })

  await test.step("remove the server", async () => {
    await page.getByRole("link", { name: "Settings" }).click()
    await page.getByRole("link", { name: /^Servers/ }).click()
    await page.getByRole("link", { name: /E2E server/ }).click()
    await page.getByRole("button", { name: "Remove server" }).click()
    await page.getByRole("region", { name: "Remove server" }).getByRole("button", { name: "Remove" }).click()
    await expect(page.getByRole("heading", { name: "No servers yet." })).toBeVisible()
  })
})
