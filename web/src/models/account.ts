import { OidcError } from "../api/oidc"
import type { CloudClient, CloudPlan, DeviceSession } from "../api/cloud"
import type { AccountSession } from "../account/session"
import { canonical, parseServerAddress } from "../lib/address"
import { ApiError, cloudMessage } from "../lib/errors"
import { replaceRoute } from "../ui/route"
import { BaseModel } from "../ui/useModel"
import { CloudDevicesModel } from "./cloudDevices"
import { hasPendingLink } from "./link"
import type { ServersModel } from "./servers"
import type { UsageModel } from "./usage"

export type AccountScreen = "unavailable" | "signedOut" | "signingIn" | "signedIn" | { failed: string }

/** The watch sign-in page: not started, opened in another tab, or something to tell the user. */
export type WatchApproval = "idle" | "opened" | { failed: string }

export interface AccountState {
  screen: AccountScreen
  /** What the last delete could not do, for the screen. */
  error: string | null
  /** A sign-out or a deletion is running (a sign-out can wait for a renewal in another tab). */
  busy: boolean
  watch: WatchApproval
  /** The Cloud's issuer is loaded: "Open approval page" can open the page inside the tap. */
  issuerReady: boolean
  /** Why the issuer could not be loaded; the screen offers a retry. */
  issuerError: string | null
  /** The account's plan, once `loadProfile` has read it (null: no plan, or not read yet; see `identities`). */
  plan: CloudPlan | null
  /** The kinds of sign-in linked to the account (`["zitadel"]`), or null until `loadProfile` has read them. */
  identities: string[] | null
  /** Why the plan and identities could not be read; the screen offers a retry. */
  profileError: string | null
  /** The invite being redeemed, or why it was not. */
  redeem: "idle" | "busy" | { failed: string }
}

/** What the account screens need from the sign-in (the real `AccountSession`, or a stand-in in a test). */
export type AccountSessionPort = Pick<
  AccountSession,
  "state" | "accountId" | "deviceId" | "beginSignIn" | "completeSignIn" | "adoptWebSession" | "signOut" | "accessToken" | "accessTokenRefused" | "onSignedOut"
> & {
  config(): Promise<{ issuer: string }>
}

/** What the account screens ask the Cloud. */
export type AccountCloud = Pick<CloudClient, "deleteAccount" | "account" | "redeemInvite" | "devices" | "removeDevice">

/**
 * What the account screens need from the agenda mirror (the `AgendaSync` of the account agenda task implements it).
 * `suspend`/`resume` hold the mirror while the account is deleted or left (like the iPhone); `invalidate` drops the work
 * queued or running. Its writes need a signed-in session, so `resume` after a sign-out leaves it idle.
 */
export interface AgendaControl {
  /** A warning to show under the account, or null. */
  readonly notice: string | null
  pushAll(): void
  suspend(): void
  resume(): void
  invalidate(): void
  /** Optional: calls the listener when `notice` changes. Returns the unsubscribe. */
  onNoticeChange?(listener: () => void): () => void
}

export interface PushHooks {
  /** The account is gone: the app drops its own push keys. */
  accountDeleted(): Promise<void>
}

export interface AccountOptions {
  /** The Cloud (built from `/config.json`, never from what a server says). */
  cloud?: AccountCloud | null
  /** Told when the account leaves, so the servers lose their account links (their tokens stay); unlinks them before a deletion. */
  servers?: Pick<ServersModel, "forgetAccount" | "unlinkAccounts"> | null
  /** Leaves the page for an address (the email sign-in page). */
  assign?: (url: string) => void
  /** The month's call minutes of a Wristcall Cloud plan (the plan row, the Agents home); forgotten when the account leaves. */
  usage?: UsageModel | null
  /** Reads the agenda again (the servers from the account, the Cloud entry): after an invite, and when Agents opens. */
  refreshAgenda?: () => void
}

const TAP_AGAIN = "Reaching wristcall Cloud. Tap again in a moment."
const BAD_USER_CODE = "Enter the 8-character code the watch shows."
const NOT_SET_UP = "wristcall Cloud isn't set up for this app."
const INVALID_INVITE = "This invite isn't valid."
const ALREADY_PLAN = "This account already has a plan."
const DELETE_FAILED = "Can't delete the account now. "
const CALLBACK_PATH = "/auth/callback"
/** Where a sign-in lands, unless a server link waited for it (`takePendingLink`). */
const HOME_PATH = "/agents"

/** Nothing read from the Cloud about the account yet. */
const NO_PROFILE = { plan: null, identities: null, profileError: null, redeem: "idle" } as const

const CODE_SHAPE = /^([A-Za-z0-9]{4})[ -]?([A-Za-z0-9]{4})$/

/**
 * The code as the identity provider shows it (`XXXX-XXXX`, upper case), or null. Unlike the Kit's rule this accepts one
 * space in the middle ("abcd efgh"): in the browser the code is typed by hand, not received from the watch.
 */
export function normalizeWatchCode(input: string): string | null {
  const match = CODE_SHAPE.exec(input.trim())
  return match === null ? null : `${match[1]}-${match[2]}`.toUpperCase()
}

/**
 * `{issuer}/device?user_code=XXXX-XXXX`, or null for a bad code or an issuer that is not an `https://` origin
 * (`http://` only for localhost, no query). The issuer is the Cloud's, never an address another party sent.
 */
export function watchSignInUrl(issuer: string, userCode: string): string | null {
  const code = normalizeWatchCode(userCode)
  const base = parseServerAddress(issuer)
  if (code === null || base === null) return null
  return `${canonical(base)}/device?user_code=${encodeURIComponent(code)}`
}

/** Short text for the screen. Never the error's own text when it could carry what was sent. */
function message(error: unknown): string {
  return error instanceof OidcError ? error.message : cloudMessage(error)
}

/** `404` and `422` are a code the Cloud does not know or no longer accepts; `409` an account that has a plan already. */
function inviteMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.status === 422) return INVALID_INVITE
    if (error.status === 409) return ALREADY_PLAN
  }
  return message(error)
}

function isSignedOut(error: unknown): boolean {
  return error instanceof OidcError && error.kind === "signed_out"
}

/**
 * The central account in this browser: sign in (email, a full-page redirect, or a device session handed over by the
 * QR sign-in), the callback, sign out, deletion, and the watch sign-in page. Optional: without a Cloud (`session` null)
 * the screen is `unavailable` and nothing here acts.
 */
export class AccountModel extends BaseModel<AccountState> {
  readonly agenda: AgendaControl | null
  /** The devices of the account (Settings); null without a Cloud. */
  readonly devices: CloudDevicesModel | null
  /** The month's call minutes of the Wristcall Cloud plan; null without a Cloud. */
  readonly usage: UsageModel | null
  readonly #session: AccountSessionPort | null
  readonly #push: PushHooks | null
  readonly #cloud: AccountCloud | null
  readonly #servers: Pick<ServersModel, "forgetAccount" | "unlinkAccounts"> | null
  readonly #assign: (url: string) => void
  readonly #refreshAgenda: (() => void) | null
  #completing: Promise<void> | null = null
  #leaving = false
  #issuer: string | null = null
  #issuerLoading: Promise<void> | null = null
  #profileLoading: Promise<void> | null = null
  /** Bumped when the account leaves: an answer for the old account is dropped. */
  #profileGeneration = 0

  constructor(session: AccountSessionPort | null, agenda: AgendaControl | null, push: PushHooks | null, opts: AccountOptions = {}) {
    super({ screen: session === null ? "unavailable" : session.state, error: null, busy: false, watch: "idle", issuerReady: false, issuerError: null, ...NO_PROFILE })
    this.#session = session
    this.agenda = agenda
    this.#push = push
    this.#cloud = opts.cloud ?? null
    this.devices = session === null || this.#cloud === null ? null : new CloudDevicesModel(this.#cloud, session)
    this.#servers = opts.servers ?? null
    this.usage = session === null ? null : (opts.usage ?? null)
    this.#assign = opts.assign ?? ((url) => globalThis.location.assign(url))
    this.#refreshAgenda = opts.refreshAgenda ?? null
    session?.onSignedOut((reason) => void this.#sessionEnded(reason))
  }

  /** At opening: a stored session mirrors the servers to the agenda. */
  start(): void {
    if (this.#session !== null && this.state.screen === "signedIn") this.agenda?.pushAll()
  }

  /** Leaves the page for the identity provider's sign-in page. */
  async signIn(): Promise<void> {
    const session = this.#session
    if (session === null || this.state.screen === "signingIn") return
    this.set({ screen: "signingIn", error: null })
    try {
      this.#assign(await session.beginSignIn())
    } catch (error) {
      this.set({ screen: { failed: message(error) } })
    }
  }

  /**
   * A device session the Cloud handed over (the QR sign-in): signed in, the agenda mirrored, and the Agents screen
   * opens (or the server link that waited for the sign-in).
   */
  async adopt(device: DeviceSession): Promise<void> {
    const session = this.#session
    if (session === null) return
    await session.adoptWebSession(device)
    this.set({ screen: "signedIn", error: null })
    this.agenda?.pushAll()
    replaceRoute(hasPendingLink() ? "/link" : HOME_PATH)
  }

  /**
   * The callback route: trades the code (once, even if called twice), clears it from the address bar and opens
   * Agents, or the server link that waited for the sign-in. A failure stays on the callback route, without the query, and shows the reason.
   */
  completeSignIn(href: string): Promise<void> {
    this.#completing ??= this.#complete(href).finally(() => {
      this.#completing = null
    })
    return this.#completing
  }

  async #complete(href: string): Promise<void> {
    const session = this.#session
    if (session === null) return
    this.set({ screen: "signingIn", error: null })
    // The code has been read (`href`): it leaves the address bar before the exchange, not after.
    history.replaceState(null, "", CALLBACK_PATH)
    try {
      await session.completeSignIn(href)
    } catch (error) {
      this.set({ screen: { failed: message(error) } })
      return
    }
    this.set({ screen: "signedIn" })
    // A server link opened before the sign-in: its screen takes it (`takePendingLink`).
    replaceRoute(hasPendingLink() ? "/link" : HOME_PATH)
    this.agenda?.pushAll()
  }

  /**
   * Deletes this browser's device at the Cloud (best effort, 2 s) and forgets the session; no provider page opens. The
   * servers and their tokens stay. Can wait for a renewal in another tab: `busy` shows it.
   */
  async signOut(): Promise<void> {
    if (this.#session === null || this.state.busy || this.state.screen !== "signedIn") return
    this.set({ busy: true, error: null })
    await this.#leave()
  }

  /**
   * `DELETE /v1/account` (the Cloud deletes the agenda and the user at the identity provider), then the app drops its
   * own push keys and signs out. On failure the session stays and `error` says so.
   */
  async deleteAccount(): Promise<void> {
    const session = this.#session
    if (session === null || this.#cloud === null || this.state.screen !== "signedIn" || this.state.busy) return
    this.set({ busy: true, error: null })
    // First: a mirror still running, or a change made meanwhile, must not add back what the deletion removes.
    this.agenda?.suspend()
    try {
      // The linked servers drop their link to the account first (best effort, like the iPhone).
      await this.#servers?.unlinkAccounts()
    } catch {
      // A server that cannot be reached keeps its link until the user unlinks it there.
    }
    let token = ""
    try {
      token = await session.accessToken()
      await this.#cloud.deleteAccount(token)
    } catch (error) {
      this.agenda?.resume()
      if (isSignedOut(error)) {
        this.set({ busy: false })
        await this.#sessionEnded(null) // the session told its reason already, if it had one
        return
      }
      this.set({ busy: false, error: DELETE_FAILED + message(error) })
      this.#refusedToken(error, token)
      return
    }
    try {
      await this.#push?.accountDeleted()
    } catch {
      // The account is gone either way; a push key left behind is harmless without it.
    }
    await this.#leave()
  }

  async #leave(): Promise<void> {
    const session = this.#session!
    this.#leaving = true
    // First: a mirror still running must not write to the agenda once the tokens are gone.
    this.agenda?.suspend()
    try {
      await session.signOut()
    } catch {
      // The session here is gone whatever happened at the Cloud.
    }
    try {
      await this.#servers?.forgetAccount()
    } catch {
      // The servers keep their old links until the next start; nothing else depends on them.
    }
    this.#leaving = false
    this.#issuer = null
    this.#issuerLoading = null
    this.#forgetProfile()
    this.set({ screen: "signedOut", busy: false, error: null, watch: "idle", issuerReady: false, issuerError: null })
    this.agenda?.resume()
  }

  /** The session ended on its own (the Cloud deleted the device, or another tab signed out): the tokens are gone already. */
  async #sessionEnded(reason: string | null): Promise<void> {
    if (this.#leaving || (this.state.screen !== "signedIn" && this.state.screen !== "signingIn")) return
    this.agenda?.suspend()
    try {
      await this.#servers?.forgetAccount()
    } catch {
      // As in `#leave`.
    }
    this.#issuer = null
    this.#issuerLoading = null
    this.#forgetProfile()
    this.set({ screen: reason === null ? "signedOut" : { failed: reason }, busy: false, error: null, watch: "idle", issuerReady: false, issuerError: null })
    this.agenda?.resume()
  }

  // Plan and identities

  /** Reads the plan and the sign-in kinds of the account (`GET /v1/account`); a read already running is shared. */
  loadProfile(): Promise<void> {
    const session = this.#session
    const cloud = this.#cloud
    if (session === null || cloud === null || this.state.screen !== "signedIn") return Promise.resolve()
    if (this.#profileLoading !== null) return this.#profileLoading
    const generation = this.#profileGeneration
    const running = (async () => {
      let token = ""
      try {
        token = await session.accessToken()
        const info = await cloud.account(token)
        if (generation === this.#profileGeneration) this.set({ plan: info.plan?.plan ? info.plan : null, identities: info.identities, profileError: null })
      } catch (error) {
        if (generation === this.#profileGeneration) this.set({ profileError: message(error) })
        this.#refusedToken(error, token)
      }
    })()
    this.#profileLoading = running
    void running.finally(() => {
      if (this.#profileLoading === running) this.#profileLoading = null
    })
    return running
  }

  /** Reads the agenda again while signed in (the Agents home opening, an invite redeemed). */
  refreshAgenda(): void {
    if (this.#session !== null && this.state.screen === "signedIn") this.#refreshAgenda?.()
  }

  /** Redeems an invite code (`POST /v1/entitlements/redeem`). True when the account has a plan now. */
  async redeemInvite(code: string): Promise<boolean> {
    const session = this.#session
    const cloud = this.#cloud
    const clean = code.trim()
    if (session === null || cloud === null || clean === "" || this.state.screen !== "signedIn" || this.state.redeem === "busy") return false
    const generation = this.#profileGeneration
    this.set({ redeem: "busy" })
    let token = ""
    try {
      token = await session.accessToken()
      const plan = await cloud.redeemInvite(token, clean)
      if (generation !== this.#profileGeneration) return false
      this.set({ plan: plan.plan ? plan : null, redeem: "idle" })
      // The Cloud adds its entry to the agenda with the plan: read it now, not at the next tab switch.
      this.refreshAgenda()
      return true
    } catch (error) {
      if (generation === this.#profileGeneration) this.set({ redeem: { failed: inviteMessage(error) } })
      this.#refusedToken(error, token)
      return false
    }
  }

  /** The Cloud refused the access token (the device was removed elsewhere, say): the session renews, or signs out. */
  #refusedToken(error: unknown, token: string): void {
    if (error instanceof ApiError && error.status === 401 && token !== "") void this.#session?.accessTokenRefused(token)
  }

  #forgetProfile(): void {
    this.#profileGeneration += 1
    this.#profileLoading = null
    this.devices?.reset()
    this.usage?.clear()
    this.set({ ...NO_PROFILE })
  }

  // Watch sign-in

  /** Reads the Cloud's issuer (once) so a tap on "Open approval page" can open the page at once. */
  loadIssuer(): Promise<void> {
    const session = this.#session
    if (session === null || this.#issuer !== null) return Promise.resolve()
    if (this.#issuerLoading !== null) return this.#issuerLoading
    const running = (async () => {
      try {
        this.#issuer = (await session.config()).issuer
        this.set({ issuerReady: true, issuerError: null, watch: typeof this.state.watch === "object" ? "idle" : this.state.watch })
      } catch (error) {
        this.set({ issuerError: message(error) })
      }
    })()
    this.#issuerLoading = running
    void running.finally(() => {
      if (this.#issuerLoading === running) this.#issuerLoading = null
    })
    return running
  }

  /**
   * Opens `{issuer}/device?user_code=…` in a new tab, inside the tap (a popup blocker refuses a window opened after an
   * await). Without the issuer a tap only starts loading it and asks to tap again. The page never comes back here.
   */
  approveWatchSignIn(userCode: string): void {
    if (this.#session === null || this.state.screen !== "signedIn") return
    if (normalizeWatchCode(userCode) === null) {
      this.set({ watch: { failed: BAD_USER_CODE } })
      return
    }
    if (this.#issuer !== null) {
      this.#openApproval(this.#issuer, userCode)
      return
    }
    void this.loadIssuer()
    this.set({ watch: { failed: TAP_AGAIN } })
  }

  #openApproval(issuer: string, userCode: string): void {
    const url = watchSignInUrl(issuer, userCode)
    if (url === null) {
      this.set({ watch: { failed: NOT_SET_UP } })
      return
    }
    window.open(url, "_blank", "noopener,noreferrer")
    this.set({ watch: "opened" })
  }

  /** A new approval screen starts clean. */
  resetWatchApproval(): void {
    if (this.state.watch !== "idle") this.set({ watch: "idle" })
  }
}
