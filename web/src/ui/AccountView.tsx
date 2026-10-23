import { useEffect, useState } from "preact/hooks"
import type { CloudPlan, CloudServer } from "../api/cloud"
import type { AccountModel } from "../models/account"
import type { CloudDevicesModel } from "../models/cloudDevices"
import { USAGE_UNAVAILABLE, usageSummary, type UsageState } from "../models/usage"
import type { NotificationsModel } from "../push/notifications"
import { Link } from "./Link"
import { NotificationsSetting } from "./NotificationsSetting"
import { useModel, type Model } from "./useModel"

/** Said by the iPhone app too: what deleting does and does not touch. */
export const DELETE_EXPLANATION =
  "Deletes your wristcall account: your sign-in and your server list in Wristcall Cloud. Your servers and their data stay as they are."

type Action = "signOut" | "delete"

/** The plan's name and its caption, as the iPhone app says them. */
export function planRow(plan: CloudPlan | null): { title: string; detail: string | null } {
  if (plan === null || !plan.plan) return { title: "No plan", detail: null }
  // "beta" (a redeemed invite) and "invite" (older ones) both read "invite"; any other source is printed as it is.
  const source = plan.source === "beta" || plan.source === "invite" ? "invite" : plan.source
  const caption = source ? `Beta · ${source}` : "Beta"
  if (plan.plan === "full") return { title: "Wristcall Cloud", detail: caption }
  if (plan.plan === "custom") return { title: "Cloud with your models", detail: caption }
  // A plan this version does not know yet: the Cloud's name for it.
  return { title: "Wristcall Cloud", detail: plan.plan }
}

const PLATFORMS: Record<string, string> = { ios: "iPhone", ipados: "iPad", macos: "Mac", watchos: "Apple Watch", web: "Web" }

/** A device name on one line (it comes from the device and may hold line breaks or control characters). */
function oneLine(name: string): string {
  const text = name.replace(/[\p{Cc}\u2028\u2029]+/gu, " ").replace(/\s+/g, " ").trim()
  return text === "" ? "Device" : text
}

/** `Redeem invite`: a field and `Redeem`, and why a code did not work. */
function RedeemInvite({ model }: { model: AccountModel }) {
  const { redeem } = useModel(model)
  const [code, setCode] = useState("")
  const busy = redeem === "busy"
  return (
    <form
      class="fields"
      onSubmit={(event) => {
        event.preventDefault()
        if (busy || code.trim() === "") return
        void model.redeemInvite(code).then((done) => {
          if (done) setCode("")
        })
      }}
    >
      <label class="field">
        <span>Redeem invite</span>
        <input type="text" name="invite" value={code} autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck={false} onInput={(event) => setCode(event.currentTarget.value)} />
      </label>
      {typeof redeem === "object" ? (
        <p class="problem" role="alert">
          {redeem.failed}
        </p>
      ) : null}
      <div class="actions">
        <button class="button" type="submit" disabled={busy || code.trim() === ""}>
          {busy ? "Redeeming…" : "Redeem"}
        </button>
      </div>
    </form>
  )
}

/** Stand-ins when the caller has no servers (so no Cloud entry) or the account no usage to read. */
const NO_ENTRY: Model<{ cloud: CloudServer | null }> = { state: { cloud: null }, subscribe: () => () => {} }
const NO_USAGE: Model<UsageState> = { state: { usage: null, unavailable: false }, subscribe: () => () => {} }

/**
 * The plan of the account, once read; or why it could not be, with a retry. With a plan, the second line says the
 * month's call minutes (or that they cannot be read now). The invite field is there only without a plan and without the
 * Wristcall Cloud entry (a lapsed plan keeps its entry).
 */
function PlanSection({ model, entry }: { model: AccountModel; entry: Model<{ cloud: CloudServer | null }> }) {
  const { plan, identities, profileError } = useModel(model)
  const { cloud } = useModel(entry)
  const { usage, unavailable } = useModel(model.usage ?? NO_USAGE)
  const hasPlan = plan !== null
  useEffect(() => {
    if (hasPlan) void model.usage?.load()
  }, [model, hasPlan])
  if (identities === null) {
    return profileError === null ? null : (
      <div>
        <p class="problem" role="alert">
          {profileError}
        </p>
        <div class="actions">
          <button class="button quiet" type="button" onClick={() => void model.loadProfile()}>
            Try again
          </button>
        </div>
      </div>
    )
  }
  const { title, detail } = planRow(plan)
  const minutes = !hasPlan ? null : usage !== null ? usageSummary(usage) : unavailable ? USAGE_UNAVAILABLE : null
  return (
    <section aria-labelledby="plan-title">
      <h3 id="plan-title">Plan</h3>
      <ul class="list" aria-label="Plan">
        <li>
          <div class="row">
            <span class="row-title">{title}</span>
            {detail !== null ? <span class="row-sub">{detail}</span> : null}
            {minutes !== null ? <span class="row-sub">{minutes}</span> : null}
          </div>
        </li>
      </ul>
      {!hasPlan && cloud === null ? <RedeemInvite model={model} /> : null}
    </section>
  )
}

/** The devices signed in to the account: this browser is marked, the others can be removed. */
function DevicesSection({ model }: { model: CloudDevicesModel }) {
  const { devices, loaded, error, removing } = useModel(model)
  useEffect(() => {
    void model.load()
  }, [model])
  if (!loaded && error === null) return null
  return (
    <section aria-labelledby="devices-title">
      <h3 id="devices-title">Devices</h3>
      {error !== null ? (
        <p class="problem" role="alert">
          {error}
        </p>
      ) : null}
      <ul class="list" aria-label="Devices">
        {devices.map((device) => (
          <li key={device.id}>
            <div class="row">
              <span class="row-title">
                {oneLine(device.name)}
                {device.thisBrowser ? (
                  <span class="pill">This browser</span>
                ) : (
                  <button class="button quiet" type="button" disabled={removing !== null} onClick={() => void model.remove(device.id)}>
                    {removing === device.id ? "Removing…" : "Remove"}
                  </button>
                )}
              </span>
              <span class="row-sub">{PLATFORMS[device.platform] ?? device.platform}</span>
            </div>
          </li>
        ))}
      </ul>
    </section>
  )
}

/**
 * The account part of Settings: sign in, sign out, delete. The caller leaves it out when the site has no Cloud. `servers`
 * tells whether the agenda lists the Wristcall Cloud entry.
 */
export function AccountSection({ model, servers = NO_ENTRY }: { model: AccountModel; servers?: Model<{ cloud: CloudServer | null }> }) {
  const { screen, error, busy, identities } = useModel(model)
  const [confirming, setConfirming] = useState(false)
  const [action, setAction] = useState<Action | null>(null)
  // The mirror's warning is not part of the model's state: re-render when it says it changed.
  const [, setNoticeTick] = useState(0)
  useEffect(() => model.agenda?.onNoticeChange?.(() => setNoticeTick((n) => n + 1)), [model])

  // The plan and the sign-in kinds are read once the account is signed in (and again for the next account).
  useEffect(() => {
    if (screen === "signedIn") void model.loadProfile()
  }, [model, screen])

  if (screen === "unavailable") return null

  async function run(which: Action, work: () => Promise<void>): Promise<void> {
    if (busy) return
    setAction(which)
    try {
      await work()
    } finally {
      setAction(null)
      setConfirming(false)
    }
  }

  const notice = model.agenda?.notice ?? null
  return (
    <section class="account" aria-labelledby="account-title">
      <h2 id="account-title">Account</h2>
      {screen === "signedIn" ? (
        <>
          <dl class="facts">
            <dt>Wristcall Cloud</dt>
            <dd>Signed in</dd>
          </dl>
          {notice !== null ? <p class="problem">{notice}</p> : null}
          {error !== null ? (
            <p class="problem" role="alert">
              {error}
            </p>
          ) : null}
          <PlanSection model={model} entry={servers} />
          {model.devices !== null ? <DevicesSection model={model.devices} /> : null}
          {/* The watch's code page is a Zitadel (email) page: only an account with an email sign-in has one to approve. */}
          {identities?.includes("zitadel") ? (
            <ul class="list" aria-label="Account">
              <li>
                <Link class="row" href="/settings/watch">
                  Approve watch sign-in
                </Link>
              </li>
            </ul>
          ) : null}
          <div class="actions">
            <button class="button quiet" type="button" disabled={busy} onClick={() => void run("signOut", () => model.signOut())}>
              {busy && action === "signOut" ? "Signing out…" : "Sign out"}
            </button>
            <button class="button danger" type="button" disabled={busy || confirming} onClick={() => setConfirming(true)}>
              Delete account
            </button>
          </div>
          {confirming ? (
            <section class="confirm" aria-label="Delete account">
              <p>{DELETE_EXPLANATION}</p>
              <div class="actions">
                <button class="button danger" type="button" disabled={busy} onClick={() => void run("delete", () => model.deleteAccount())}>
                  {busy && action === "delete" ? "Deleting…" : "Delete account"}
                </button>
                <button class="button quiet" type="button" disabled={busy} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </div>
            </section>
          ) : null}
        </>
      ) : screen === "signingIn" ? (
        <p>Signing in…</p>
      ) : (
        <>
          {typeof screen === "object" ? (
            <p class="problem" role="alert">
              {screen.failed}
            </p>
          ) : null}
          <ul class="list" aria-label="Sign in">
            <li>
              <Link class="row" href="/sign-in">
                Sign in
              </Link>
            </li>
          </ul>
          <p class="footnote">Sign in to add servers with a pairing code and to see them on your watch.</p>
        </>
      )}
    </section>
  )
}

/** Stands in for the pending requests when the caller has none to show. */
const NO_REQUESTS: Model<{ count: number }> = { state: { count: 0 }, subscribe: () => () => {} }

/** Where the servers and the device requests live: both are advanced, so they sit in Settings. */
function AdvancedSection({ requests }: { requests: Model<{ count: number }> }) {
  const { count } = useModel(requests)
  return (
    <>
      <ul class="list" aria-label="Servers and requests">
        <li>
          <Link class="row" href="/settings/servers">
            <span class="row-title">
              Servers
              <span class="muted">Advanced</span>
            </span>
          </Link>
        </li>
        <li>
          <Link class="row" href="/approvals">
            <span class="row-title">
              Device requests
              {count > 0 ? <span class="row-action">{count}</span> : null}
            </span>
          </Link>
        </li>
      </ul>
    </>
  )
}

/**
 * Settings: the account section appears only when the site has a Cloud, and notifications only when it also sends Web
 * Push. `badge` counts the pending device requests.
 */
export function SettingsView({
  account,
  notifications = null,
  badge = NO_REQUESTS,
  servers = NO_ENTRY,
}: {
  account: AccountModel | null
  notifications?: NotificationsModel | null
  badge?: Model<{ count: number }>
  servers?: Model<{ cloud: CloudServer | null }>
}) {
  return (
    <main class="screen">
      <h1>Settings</h1>
      {account !== null ? <AccountSection model={account} servers={servers} /> : null}
      {notifications !== null ? <NotificationsSetting model={notifications} /> : null}
      <AdvancedSection requests={badge} />
    </main>
  )
}
