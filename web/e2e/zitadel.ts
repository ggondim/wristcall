import type { Locator, Page } from "@playwright/test"

// Plays the human at Zitadel's hosted login (login v2, a Next.js app) for the test user: same logic as
// `tools/e2e/zitadel_browser.py`. Waits are by time, not by page loads: a submit runs a server action and may keep the
// old page on screen while it works. The password is only ever passed to `fill`, and a failure never carries
// Playwright's call log (it holds the filled value).

const SKIP_LABELS = ["Skip", "Überspringen", "Not now", "Later", "Pular", "Agora não"]
const ALLOW_LABELS = ["Allow", "Erlauben", "Permitir", "Accept", "Akzeptieren", "Aceitar"]
// Where a login error shows. Never a bare `[role=alert]`: Next.js's route announcer says the page title with it.
const ERROR_SELECTOR = ".lgn-error, form [data-testid=error], form [role=alert], form .error, form [data-error]"
const ANSWER_WITHIN_MS = 10_000
const STUCK_AFTER_MS = 30_000

async function visible(page: Page, selector: string): Promise<Locator | null> {
  for (const element of await page.locator(selector).all()) {
    if (await element.isVisible()) return element
  }
  return null
}

async function button(page: Page, labels: string[]): Promise<Locator | null> {
  for (const label of labels) {
    const element = await visible(page, `button:text-is('${label}'), input[type=submit][value='${label}']`)
    if (element !== null) return element
  }
  return null
}

async function submit(page: Page, field: Locator): Promise<void> {
  const target = (await visible(page, "#submit-button")) ?? (await visible(page, "button[type=submit]:not([name=skip])"))
  if (target !== null) await target.click()
  else await field.press("Enter")
}

/** True while the form is submitting: v2 disables the submit button until the server action answers. A disabled button over an empty field only means "type something". */
async function busy(page: Page): Promise<boolean> {
  const target = await visible(page, "button[type=submit]:not([name=skip])")
  if (target === null) return false
  try {
    return await target.evaluate(
      (b) =>
        (b as HTMLButtonElement).disabled &&
        !!(b as HTMLButtonElement).form &&
        [...(b as HTMLButtonElement).form!.querySelectorAll("input")]
          .filter((i) => i.type !== "hidden" && i.offsetParent !== null)
          .every((i) => (i.type === "checkbox" || i.type === "radio" ? i.checked : !!i.value)),
      undefined,
      { timeout: 2_000 },
    )
  } catch {
    return false // the page changed under us; the next round looks again
  }
}

function pathOf(url: string): string {
  const parts = new URL(url)
  return `${parts.origin}${parts.pathname}`
}

/**
 * Signs the test user in at the page that is showing Zitadel's login (or will be: call right after the click that
 * leaves the site). Returns once the browser is back on the site's own origin.
 */
export async function signInAtZitadel(page: Page, opts: { user: string; password: string; appOrigin: string; timeoutMs?: number }): Promise<void> {
  const deadline = Date.now() + (opts.timeoutMs ?? 90_000)
  let sentUser = 0
  let sentPassword = 0
  let last = ""
  let since = Date.now()
  for (;;) {
    if (Date.now() > deadline) throw new Error(`timed out on ${pathOf(page.url())}`)
    // Back on the site (the callback runs there): done.
    if (new URL(page.url()).origin === opts.appOrigin) return
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => undefined)
    if (new URL(page.url()).origin === opts.appOrigin) return
    const here = pathOf(page.url())
    const now = Date.now()
    if (here !== last) {
      last = here
      since = now
    } else if (now - since > STUCK_AFTER_MS) {
      throw new Error(`stuck on ${here}`)
    }
    try {
      const body = (await page.locator("body").count()) > 0 ? await page.locator("body").innerText({ timeout: 5_000 }) : ""
      if (body.replace(/ /g, "").includes('"code":5') && body.includes("Not Found")) {
        throw new Error(`the login page ${here} answers 404 (the hosted login is down)`)
      }
      const error = await visible(page, ERROR_SELECTOR)
      if (error !== null) {
        const text = (await error.innerText()).trim()
        if (text !== "") throw new Error(`login error on ${here}: ${text.slice(0, 200)}`)
      }
      if (await busy(page)) {
        // The answer window counts from when the form goes idle, not from the submit.
        if (sentUser > 0) sentUser = Date.now()
        if (sentPassword > 0) sentPassword = Date.now()
        await page.waitForTimeout(500)
        continue
      }
      const password = await visible(page, "input[type=password]")
      if (password !== null) {
        if (Date.now() - sentPassword < ANSWER_WITHIN_MS) {
          await page.waitForTimeout(500)
          continue
        }
        if (sentPassword > 0) throw new Error(`password not accepted on ${here}`)
        try {
          await password.fill(opts.password)
        } catch {
          throw new Error("password field not fillable")
        }
        sentPassword = Date.now()
        await submit(page, password)
        continue
      }
      const name = await visible(page, "#loginName, input[name=loginName], input[autocomplete=username]")
      if (name !== null) {
        if (Date.now() - sentUser < ANSWER_WITHIN_MS) {
          await page.waitForTimeout(500)
          continue
        }
        if (sentUser > 0) throw new Error(`login name not accepted on ${here}`)
        await name.fill(opts.user)
        sentUser = Date.now()
        await submit(page, name)
        continue
      }
      const skip = (await visible(page, "button[name=skip]")) ?? (await button(page, SKIP_LABELS))
      if (skip !== null) {
        await skip.click()
        continue
      }
      const allow = (await visible(page, "button[formaction='./allowed']")) ?? (await button(page, ALLOW_LABELS))
      if (allow !== null) {
        await allow.click()
        continue
      }
      await page.waitForTimeout(1_000)
    } catch (e) {
      // A page that changes while it is read is not an error: look again. Anything this helper threw ends the run.
      const message = e instanceof Error ? e.message : ""
      if (/^(timed out|stuck|the login page|login error|password not|login name not|password field)/.test(message)) throw e
      await page.waitForTimeout(500)
    }
  }
}
