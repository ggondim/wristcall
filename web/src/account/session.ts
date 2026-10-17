import { type CloudClient, type CloudConfig, type DeviceSession } from "../api/cloud"
import {
  type PendingLogin,
  type Provider,
  OidcError,
  PENDING_MAX_AGE_MS,
  authorizationRequest,
  discover,
  exchange,
  parseCallback,
  refresh,
  revoke,
} from "../api/oidc"
import { browserDeviceName } from "../lib/deviceName"
import { ApiError } from "../lib/errors"
import { newId } from "../lib/ids"
import { type ServerStore } from "../store/servers"

export type SessionState = "signedOut" | "signedIn"

/** The Cloud `web` device of this browser and its tokens; `expires_at` is the access token's (epoch seconds). */
const KEY_DEVICE = "account.device"
/** The Zitadel tokens the PWA 0.2.x kept: only read to migrate to a device, then deleted. */
const KEY_REFRESH = "account.refresh"
const KEY_ID_TOKEN = "account.id_token"
const KEY_PENDING = "oidc.pending"
/** Stands in for the Web Lock in browsers without one: `{owner, until}`. */
const KEY_LEASE = "refresh.lease"
const REFRESH_LOCK = "wc.refresh"
/** An access token with less than this to live is renewed. */
const FRESH_MS = 60_000
/** Longest the sign-out waits for the Cloud to delete the device. */
const SIGN_OUT_WAIT_MS = 2_000
/** Longest the opening waits for the migration of a Zitadel session (the first render waits for `load`). */
const MIGRATION_WAIT_MS = 5_000
const LEASE_MS = 30_000
/** A renewal may take up to 60 s: the lease is extended while it runs. */
const LEASE_EXTEND_MS = 10_000
const LEASE_POLL_MS = 50
const NOT_CONFIGURED = "This wristcall Cloud doesn't accept sign-in from the web app yet."
const SIGNED_OUT = "You are signed out."
/** The Cloud deleted the device: removed elsewhere, or a refresh token used twice (a lost answer to a rotation). */
const REVOKED = "This browser was signed out. Sign in again."

interface Lease {
  owner: string
  until: number
}

/** What this tab holds in memory: never the refresh token, which is always read from IndexedDB. */
interface Held {
  account: string
  device: string
  accessToken: string
  /** Milliseconds since the epoch. */
  expiresAt: number
}

/** What is kept of a device session: only its fields (the exchange also answers `key_set`). */
function deviceRecord(session: DeviceSession): DeviceSession {
  const { account, device, access_token, refresh_token, expires_at } = session
  return { account, device, access_token, refresh_token, expires_at }
}

function deviceRefused(error: unknown): boolean {
  return error instanceof ApiError && error.status === 401 && (error.code === "revoked" || error.code === "unauthorized")
}

/** This browser's name as a Cloud device. */
function thisBrowser(): string {
  return browserDeviceName(globalThis.navigator?.userAgent ?? "")
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * The central account in this browser, as a Cloud `web` device: reads the Cloud's config, keeps the access token fresh
 * with `POST /v1/session/refresh` and signs out. The refresh token rotates and reusing one deletes the device, so a
 * renewal runs one at a time across tabs (the `wc.refresh` Web Lock, or a lease in IndexedDB) and always sends the
 * token read from IndexedDB inside that lock. The device and both its tokens live in IndexedDB, so a new tab uses the
 * stored access token instead of rotating. Zitadel (OIDC with PKCE) only serves "Sign in with email", traded at once for
 * a device; a Zitadel session left by the PWA 0.2.x is migrated on `load`.
 */
export class AccountSession {
  readonly #cloud: CloudClient
  readonly #store: ServerStore
  readonly #fetch: typeof fetch | undefined
  readonly #now: () => number
  readonly #origin: string
  readonly #listeners = new Set<(reason: string | null) => void>()
  readonly #changeListeners = new Set<() => void>()

  #state: SessionState = "signedOut"
  #config: Promise<CloudConfig> | null = null
  #oidc: Promise<{ clientId: string; provider: Provider }> | null = null
  #held: Held | null = null
  #refreshing: Promise<string> | null = null
  /** An access token the Cloud answered `401` to: not used again, even when it looks fresh. */
  #refused: string | null = null
  /** Bumped by sign-in, sign-out and an ended session: a renewal that started before must not write over them. */
  #generation = 0

  constructor(cloud: CloudClient, store: ServerStore, opts: { fetchImpl?: typeof fetch; now?: () => number; origin?: string } = {}) {
    this.#cloud = cloud
    this.#store = store
    this.#fetch = opts.fetchImpl
    this.#now = opts.now ?? Date.now
    this.#origin = opts.origin ?? globalThis.location.origin
  }

  get state(): SessionState {
    return this.#state
  }

  /** The account of the device this browser holds, or null when signed out. */
  get accountId(): string | null {
    return this.#held?.account ?? null
  }

  /** The Cloud device of this browser, or null when signed out. */
  get deviceId(): string | null {
    return this.#held?.device ?? null
  }

  /** The Cloud's client: the agenda mirror calls it with this session's access token. */
  get cloudClient(): CloudClient {
    return this.#cloud
  }

  /** The Cloud's address: what a linked server names as its account service (`health.account.issuer`). */
  get cloudUrl(): string {
    return this.#cloud.url
  }

  /** A token that proves the account to one server (`audience`: its address). Needs a fresh access token. */
  async serverToken(audience: string): Promise<{ token: string; audience: string; expires_at: number }> {
    const token = await this.accessToken()
    try {
      return await this.#cloud.serverToken(token, audience)
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) await this.accessTokenRefused(token)
      throw error
    }
  }

  /**
   * Reads the device from the database: signed in when there is one. Without one, a Zitadel session of the PWA 0.2.x
   * becomes a device (waiting at most 5 s; past that, or when it fails, this opening stays signed out).
   */
  async load(): Promise<void> {
    const before = this.#state
    let found = await this.#store.getKV<DeviceSession>(KEY_DEVICE)
    if (found !== null) await this.#dropZitadelTokens()
    else if ((await this.#store.getKV<string>(KEY_REFRESH)) !== null) {
      found = await this.#withinTime(this.#underLock(() => this.#migrateLocked()), MIGRATION_WAIT_MS, null)
    }
    if (found === null) {
      this.#held = null
      this.#state = "signedOut"
    } else {
      this.#hold(found)
      this.#state = "signedIn"
    }
    if (this.#state !== before) this.#notifyChange()
  }

  /** `GET /v1/config`, kept in memory after the first success. */
  config(): Promise<CloudConfig> {
    if (this.#config === null) {
      // The promise is cached, so concurrent callers share one request; a failure is forgotten.
      const running = this.#cloud.config()
      this.#config = running
      running.catch(() => {
        if (this.#config === running) this.#config = null
      })
    }
    return this.#config
  }

  /** Saves the pending login (IndexedDB, single use) and returns the authorization URL to open. */
  async beginSignIn(): Promise<string> {
    const { clientId, provider, config } = await this.#provider()
    const { url, pending } = await authorizationRequest(provider, clientId, `${this.#origin}/auth/callback`, config.scopes, this.#now)
    await this.#store.setKV(KEY_PENDING, pending)
    return url
  }

  /**
   * The email callback: trades the code for Zitadel tokens, then the access token for a `web` device
   * (`POST /v1/devices`). The Zitadel tokens are not kept (their refresh token is revoked, best effort). The pending
   * login is taken (read and erased in one step) before anything else: a callback that fails or repeats never gets a
   * second try.
   */
  async completeSignIn(href: string): Promise<void> {
    const pending = await this.#store.takeKV<PendingLogin>(KEY_PENDING)
    if (pending === null) throw new OidcError("state", "There is no sign-in in progress. Start again.")
    if (this.#now() - pending.createdAt > PENDING_MAX_AGE_MS) throw new OidcError("state", "The sign-in took too long. Start again.")
    const { clientId, provider } = await this.#provider()
    const { code } = parseCallback(href, pending, provider.issuer)
    const tokens = await exchange(provider, clientId, code, pending, this.#fetch, this.#now)
    let created: DeviceSession
    try {
      created = deviceRecord(await this.#cloud.exchangeSignIn(tokens.access_token, thisBrowser()))
    } finally {
      this.#revokeQuietly(tokens.refresh_token)
    }
    await this.#signIn(created)
  }

  /** A device session handed over by the Cloud (the QR sign-in): stored, held and signed in. */
  async adoptWebSession(session: DeviceSession): Promise<void> {
    await this.#signIn(deviceRecord(session))
  }

  /** An access token with at least 60 s to live. Renews when needed, one renewal at a time. */
  accessToken(): Promise<string> {
    if (this.#state !== "signedIn") return Promise.reject(new OidcError("signed_out", SIGNED_OUT))
    const held = this.#held
    if (held !== null && held.accessToken !== this.#refused && held.expiresAt - this.#now() >= FRESH_MS) return Promise.resolve(held.accessToken)
    if (this.#refreshing === null) {
      const running = this.#underLock(() => this.#renewLocked()).finally(() => {
        if (this.#refreshing === running) this.#refreshing = null
      })
      this.#refreshing = running
    }
    return this.#refreshing
  }

  /**
   * The Cloud answered `401` to `token`: renews (or takes the one another tab stored). A refused renewal signs out;
   * any other failure leaves the session for the next call.
   */
  async accessTokenRefused(token: string): Promise<void> {
    if (this.#state !== "signedIn") return
    this.#refused = token
    try {
      await this.accessToken()
    } catch {
      // Signed out already when the renewal was refused; nothing to tell otherwise.
    }
  }

  /**
   * Deletes this device at the Cloud (renewing the access token first when needed, all within 2 s), then forgets the
   * session. Always null: there is no provider page to open (the Zitadel browser session stays, for "Sign in with email").
   */
  async signOut(): Promise<null> {
    const device = this.#state === "signedIn" ? (this.#held?.device ?? null) : null
    if (device !== null) {
      const removal = (async () => this.#cloud.removeDevice(await this.accessToken(), device))()
      await this.#withinTime(removal, SIGN_OUT_WAIT_MS, undefined)
    }
    this.#generation += 1
    this.#refreshing = null
    this.#refused = null
    this.#forget(null)
    await this.#store.deleteKV(KEY_DEVICE)
    await this.#dropZitadelTokens()
    return null
  }

  /** Calls `listener` whenever `state` changes (signed in, signed out or ended by the Cloud). Returns the unsubscribe. */
  onStateChange(listener: () => void): () => void {
    this.#changeListeners.add(listener)
    return () => {
      this.#changeListeners.delete(listener)
    }
  }

  /** Calls `listener` once per sign-in when it ends, with the text to show (null when there is nothing to tell). */
  onSignedOut(listener: (reason: string | null) => void): () => void {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }

  async #provider(): Promise<{ clientId: string; provider: Provider; config: CloudConfig }> {
    const config = await this.config()
    if (this.#oidc === null) {
      const clientId = config.clients.pwa
      if (clientId === undefined || clientId === "") throw new OidcError("invalid", NOT_CONFIGURED)
      const running = discover(config.issuer, this.#fetch).then((provider) => ({ clientId, provider }))
      this.#oidc = running
      running.catch(() => {
        if (this.#oidc === running) this.#oidc = null
      })
    }
    return { ...(await this.#oidc), config }
  }

  /** A new device replaces whatever this browser had (Zitadel leftovers included, so they are never migrated after). */
  async #signIn(device: DeviceSession): Promise<void> {
    this.#generation += 1
    this.#refreshing = null
    this.#refused = null
    await this.#store.setKV(KEY_DEVICE, device)
    await this.#dropZitadelTokens()
    this.#hold(device)
    this.#state = "signedIn"
    this.#notifyChange()
  }

  #hold(device: DeviceSession): void {
    this.#held = { account: device.account, device: device.device, accessToken: device.access_token, expiresAt: device.expires_at * 1000 }
  }

  /** Forgets what is held in memory and tells the listeners (once per sign-in). */
  #forget(reason: string | null): void {
    const wasSignedIn = this.#state === "signedIn"
    this.#held = null
    this.#state = "signedOut"
    if (wasSignedIn) {
      for (const listener of [...this.#listeners]) listener(reason)
      this.#notifyChange()
    }
  }

  #notifyChange(): void {
    for (const listener of [...this.#changeListeners]) listener()
  }

  async #dropZitadelTokens(): Promise<void> {
    await this.#store.deleteKV(KEY_REFRESH)
    await this.#store.deleteKV(KEY_ID_TOKEN)
  }

  /** Revokes a Zitadel token nobody will use, without waiting and without telling anyone if it fails. */
  #revokeQuietly(token: string | null): void {
    if (token === null) return
    void (async () => {
      try {
        const { clientId, provider } = await this.#provider()
        await revoke(provider, clientId, token, this.#fetch)
      } catch {
        // Ignored on purpose; nothing is logged.
      }
    })()
  }

  /** Deletes a device nobody holds (its session lost to a sign-out or a newer sign-in), best effort. */
  #removeQuietly(device: DeviceSession): void {
    void this.#cloud.removeDevice(device.access_token, device.device).catch(() => {
      // Ignored on purpose; nothing is logged.
    })
  }

  /** `work`'s result, or `fallback` when it fails or takes longer than `ms` (it keeps running). */
  async #withinTime<T>(work: Promise<T>, ms: number, fallback: T): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | undefined
    const limit = new Promise<T>((resolve) => {
      timer = setTimeout(() => resolve(fallback), ms)
    })
    try {
      return await Promise.race([work.catch(() => fallback), limit])
    } finally {
      clearTimeout(timer)
    }
  }

  /** Inside the refresh Web Lock, or the IndexedDB lease without one: two tabs must not send the same refresh token. */
  async #underLock<T>(work: () => Promise<T>): Promise<T> {
    const locks = (globalThis.navigator as Navigator | undefined)?.locks as LockManager | null | undefined
    if (locks === undefined || locks === null) return this.#underLease(work)
    return await locks.request(REFRESH_LOCK, work)
  }

  /** Takes `refresh.lease` (free, expired or already ours) in one transaction, waiting while another tab holds it. */
  async #underLease<T>(work: () => Promise<T>): Promise<T> {
    const owner = newId()
    const claim = (current: Lease | null): Lease | undefined =>
      current === null || current.owner === owner || current.until <= this.#now() ? { owner, until: this.#now() + LEASE_MS } : undefined
    while ((await this.#store.swapKV<Lease>(KEY_LEASE, claim))?.owner !== owner) await sleep(LEASE_POLL_MS)
    const extend = setInterval(() => {
      void this.#store.swapKV<Lease>(KEY_LEASE, (current) => (current?.owner === owner ? claim(current) : undefined)).catch(() => {})
    }, LEASE_EXTEND_MS)
    try {
      return await work()
    } finally {
      clearInterval(extend)
      await this.#store.swapKV<Lease>(KEY_LEASE, (current) => (current?.owner === owner ? null : undefined)).catch(() => {})
    }
  }

  async #renewLocked(): Promise<string> {
    const generation = this.#generation
    // Another tab may have renewed meanwhile, and the Cloud deletes the device on a reused token: always the stored one.
    const current = await this.#store.getKV<DeviceSession>(KEY_DEVICE)
    if (current === null) return this.#endSession(generation, null) // signed out in another tab
    if (current.access_token !== this.#refused && current.expires_at * 1000 - this.#now() >= FRESH_MS) return this.#use(generation, current)
    let renewed: DeviceSession
    try {
      renewed = deviceRecord(await this.#cloud.refreshSession(current.refresh_token))
    } catch (error) {
      if (!deviceRefused(error)) throw error
      // No second try: the Cloud deleted the device already.
      await this.#store.swapKV<DeviceSession>(KEY_DEVICE, (now) => (now?.refresh_token === current.refresh_token ? null : undefined))
      return this.#endSession(generation, REVOKED)
    }
    // Stored only over the session it renewed: a sign-out (or a new sign-in) while the request was out wins.
    const kept = await this.#store.swapKV<DeviceSession>(KEY_DEVICE, (now) =>
      now !== null && now.device === current.device && now.refresh_token === current.refresh_token ? renewed : undefined,
    )
    if (kept === null || kept.device !== renewed.device) this.#removeQuietly(renewed) // nobody holds this device now
    if (kept === null) return this.#endSession(generation, null)
    return this.#use(generation, kept)
  }

  /** The stored session's access token, held unless a sign-in or sign-out of this tab finished meanwhile. */
  #use(generation: number, device: DeviceSession): string {
    if (generation === this.#generation) {
      this.#hold(device)
      return device.access_token
    }
    if (this.#state === "signedIn" && this.#held !== null) return this.#held.accessToken // a new sign-in won
    throw new OidcError("signed_out", SIGNED_OUT)
  }

  async #endSession(generation: number, reason: string | null): Promise<never> {
    if (generation === this.#generation) {
      this.#generation += 1
      this.#refreshing = null
      this.#forget(reason)
    }
    throw new OidcError("signed_out", reason ?? SIGNED_OUT)
  }

  /** Inside the lock: a Zitadel session of the PWA 0.2.x becomes a `web` device. Null leaves this opening signed out. */
  async #migrateLocked(): Promise<DeviceSession | null> {
    const migrated = await this.#store.getKV<DeviceSession>(KEY_DEVICE)
    if (migrated !== null) {
      // Another tab migrated first.
      await this.#dropZitadelTokens()
      return migrated
    }
    const old = await this.#store.getKV<string>(KEY_REFRESH)
    if (old === null) return null
    let renewed: { access: string; refresh: string }
    try {
      // A renewed access token: the Cloud refuses one issued more than 600 s ago (`stale_token`).
      const { clientId, provider } = await this.#provider()
      const tokens = await refresh(provider, clientId, old, this.#fetch, this.#now)
      renewed = { access: tokens.access_token, refresh: tokens.refresh_token ?? old }
    } catch (error) {
      // Refused by Zitadel: nothing to migrate, nothing created. Anything else: tried again at the next opening.
      if (error instanceof OidcError && error.kind === "signed_out") await this.#dropZitadelTokens()
      return null
    }
    // Zitadel rotated the refresh token: kept first, so a failure below leaves the live one for the next opening.
    if (renewed.refresh !== old) await this.#store.setKV(KEY_REFRESH, renewed.refresh)
    let created: DeviceSession
    try {
      created = deviceRecord(await this.#cloud.exchangeSignIn(renewed.access, thisBrowser()))
    } catch {
      return null // the device limit, a stale token, the Cloud busy or offline: the old session stays
    }
    // Only into an empty slot: a sign-in that finished meanwhile (after the 5 s) wins, and this device is not kept.
    const kept = await this.#store.swapKV<DeviceSession>(KEY_DEVICE, (now) => (now === null ? created : undefined))
    if (kept !== created) this.#removeQuietly(created)
    await this.#dropZitadelTokens()
    this.#revokeQuietly(renewed.refresh)
    return kept
  }
}
