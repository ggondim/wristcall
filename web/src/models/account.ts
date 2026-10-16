import { OidcError } from "../api/oidc"
import type { CloudClient } from "../api/cloud"
import type { AccountSession } from "../account/session"
import { canonical, parseServerAddress } from "../lib/address"
import { ApiError, NetworkError, TimeoutError } from "../lib/errors"
import { replaceRoute } from "../ui/route"
import { BaseModel } from "../ui/useModel"
import { hasPendingLink } from "./link"
import type { ServersModel } from "./servers"

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
}

/** What the account screens need from the sign-in (the real `AccountSession`, or a stand-in in a test). */
export type AccountSessionPort = Pick<AccountSession, "state" | "beginSignIn" | "completeSignIn" | "signOut" | "accessToken" | "onSignedOut"> & {
  config(): Promise<{ issuer: string }>
}

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
  /** The Cloud, for `DELETE /v1/account` (built from `/config.json`, never from what a server says). */
  cloud?: Pick<CloudClient, "deleteAccount"> | null
  /** Told when the account leaves, so the servers lose their account links (their tokens stay). */
  servers?: Pick<ServersModel, "forgetAccount"> | null
  /** Leaves the page for an address (the sign-in page, the end-session page). */
  assign?: (url: string) => void
}

const TAP_AGAIN = "Reaching wristcall Cloud. Tap again in a moment."
const BAD_USER_CODE = "Enter the 8-character code the watch shows."
const NOT_SET_UP = "wristcall Cloud isn't set up for this app."
const CLOUD_DOWN = "Can't reach wristcall Cloud."
const DELETE_FAILED = "Can't delete the account now. "
const CALLBACK_PATH = "/auth/callback"

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
  if (error instanceof OidcError) return error.message
  if (error instanceof NetworkError || error instanceof TimeoutError) return CLOUD_DOWN
  if (error instanceof ApiError) {
    return error.status === 503 || error.status === 502 ? "wristcall Cloud is busy. Try again in a moment." : "wristcall Cloud refused the request."
  }
  return "Something went wrong. Try again."
}

function isSignedOut(error: unknown): boolean {
  return error instanceof OidcError && error.kind === "signed_out"
}

/**
 * The central account in this browser: sign in (a full-page redirect), the callback, sign out, deletion, and the
 * watch sign-in page. Optional: without a Cloud (`session` null) the screen is `unavailable` and nothing here acts.
 */
export class AccountModel extends BaseModel<AccountState> {
  readonly agenda: AgendaControl | null
  readonly #session: AccountSessionPort | null
  readonly #push: PushHooks | null
  readonly #cloud: Pick<CloudClient, "deleteAccount"> | null
  readonly #servers: Pick<ServersModel, "forgetAccount"> | null
  readonly #assign: (url: string) => void
  #completing: Promise<void> | null = null
  #leaving = false
  #issuer: string | null = null
  #issuerLoading: Promise<void> | null = null

  constructor(session: AccountSessionPort | null, agenda: AgendaControl | null, push: PushHooks | null, opts: AccountOptions = {}) {
    super({ screen: session === null ? "unavailable" : session.state, error: null, busy: false, watch: "idle", issuerReady: false, issuerError: null })
    this.#session = session
    this.agenda = agenda
    this.#push = push
    this.#cloud = opts.cloud ?? null
    this.#servers = opts.servers ?? null
    this.#assign = opts.assign ?? ((url) => globalThis.location.assign(url))
    session?.onSignedOut(() => void this.#sessionEnded())
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
   * The callback route: trades the code (once, even if called twice), clears it from the address bar and opens
   * Settings, or the server link that waited for the sign-in. A failure stays on the callback route, without the query, and shows the reason.
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
    replaceRoute(hasPendingLink() ? "/link" : "/settings")
    this.agenda?.pushAll()
  }

  /**
   * Deletes the tokens, revokes the refresh token and opens the provider's end-session page (so the next sign-in can
   * pick another user). The servers and their tokens stay. Can wait for a renewal in another tab: `busy` shows it.
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
      await this.#cloud.deleteAccount(await session.accessToken())
    } catch (error) {
      this.agenda?.resume()
      if (isSignedOut(error)) {
        this.set({ busy: false })
        await this.#sessionEnded()
        return
      }
      this.set({ busy: false, error: DELETE_FAILED + message(error) })
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
    let endSession: string | null = null
    try {
      endSession = await session.signOut()
    } catch {
      // The tokens here are gone whatever happened at the provider.
    }
    try {
      await this.#servers?.forgetAccount()
    } catch {
      // The servers keep their old links until the next start; nothing else depends on them.
    }
    this.#leaving = false
    this.#issuer = null
    this.#issuerLoading = null
    this.set({ screen: "signedOut", busy: false, error: null, watch: "idle", issuerReady: false, issuerError: null })
    this.agenda?.resume()
    // The page leaves for the provider's end-session page; the app is signed out already.
    if (endSession !== null) this.#assign(endSession)
  }

  /** The session ended on its own (the renewal was refused): the tokens are gone already. */
  async #sessionEnded(): Promise<void> {
    if (this.#leaving || this.state.screen === "signedOut" || this.state.screen === "unavailable") return
    this.agenda?.suspend()
    try {
      await this.#servers?.forgetAccount()
    } catch {
      // As in `#leave`.
    }
    this.#issuer = null
    this.#issuerLoading = null
    this.set({ screen: "signedOut", busy: false, error: null, watch: "idle", issuerReady: false, issuerError: null })
    this.agenda?.resume()
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
