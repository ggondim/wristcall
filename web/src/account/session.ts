import { type CloudClient, type CloudConfig } from "../api/cloud"
import {
  type PendingLogin,
  type Provider,
  type Tokens,
  OidcError,
  PENDING_MAX_AGE_MS,
  authorizationRequest,
  discover,
  endSessionUrl,
  exchange,
  parseCallback,
  refresh,
  revoke,
} from "../api/oidc"
import { type ServerStore } from "../store/servers"

export type SessionState = "signedOut" | "signedIn"

const KEY_REFRESH = "account.refresh"
const KEY_ID_TOKEN = "account.id_token"
const KEY_PENDING = "oidc.pending"
const REFRESH_LOCK = "wc.refresh"
/** An access token with less than this to live is renewed. */
const FRESH_MS = 60_000
/** Longest the sign-out waits for the revocation before handing back the end-session URL. */
const REVOKE_WAIT_MS = 2_000
const NOT_CONFIGURED = "This wristcall Cloud doesn't accept sign-in from the web app yet."

/**
 * Owns the central account's tokens in this browser: reads the Cloud's config, keeps the access token fresh (one
 * renewal at a time, across tabs when the browser has Web Locks) and signs out. The refresh token and the ID token
 * live in IndexedDB; the access token only in memory.
 */
export class AccountSession {
  readonly #cloud: CloudClient
  readonly #store: ServerStore
  readonly #fetch: typeof fetch | undefined
  readonly #now: () => number
  readonly #origin: string
  readonly #listeners = new Set<() => void>()

  #state: SessionState = "signedOut"
  #config: CloudConfig | null = null
  #oidc: { clientId: string; provider: Provider } | null = null
  #access: { token: string; expiresAt: number } | null = null
  #refreshing: Promise<string> | null = null
  /** Bumped by sign-in, sign-out and a refused renewal: a renewal that started before must not write over them. */
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

  /** Reads the refresh token from the database: signed in when there is one. */
  async load(): Promise<void> {
    this.#state = (await this.#store.getKV<string>(KEY_REFRESH)) ? "signedIn" : "signedOut"
  }

  /** `GET /v1/config`, kept in memory after the first success. */
  async config(): Promise<CloudConfig> {
    if (this.#config !== null) return this.#config
    const config = await this.#cloud.config()
    this.#config = config
    return config
  }

  /** Saves the pending login (IndexedDB, single use) and returns the authorization URL to open. */
  async beginSignIn(): Promise<string> {
    const { clientId, provider, config } = await this.#provider()
    const { url, pending } = await authorizationRequest(provider, clientId, `${this.#origin}/auth/callback`, config.scopes, this.#now)
    await this.#store.setKV(KEY_PENDING, pending)
    return url
  }

  /**
   * Trades the callback's code for tokens. The pending login is taken (read and erased in one step) before anything
   * else: a callback that fails or repeats never gets a second try.
   */
  async completeSignIn(href: string): Promise<void> {
    const pending = await this.#store.takeKV<PendingLogin>(KEY_PENDING)
    if (pending === null) throw new OidcError("state", "There is no sign-in in progress. Start again.")
    if (this.#now() - pending.createdAt > PENDING_MAX_AGE_MS) throw new OidcError("state", "The sign-in took too long. Start again.")
    const { clientId, provider } = await this.#provider()
    const { code } = parseCallback(href, pending, provider.issuer)
    const tokens = await exchange(provider, clientId, code, pending, this.#fetch, this.#now)
    this.#generation += 1
    this.#refreshing = null
    await this.#save(tokens)
    this.#state = "signedIn"
  }

  /** An access token with at least 60 s to live. Renews when needed, one renewal at a time. */
  accessToken(): Promise<string> {
    if (this.#state !== "signedIn") return Promise.reject(new OidcError("signed_out", "You are signed out."))
    if (this.#access !== null && this.#access.expiresAt - this.#now() >= FRESH_MS) return Promise.resolve(this.#access.token)
    if (this.#refreshing === null) {
      const running = this.#renew().finally(() => {
        if (this.#refreshing === running) this.#refreshing = null
      })
      this.#refreshing = running
    }
    return this.#refreshing
  }

  /**
   * Deletes the tokens, revokes the refresh token (waiting at most 2 s, with `keepalive`, so a navigation right after
   * does not cut it) and returns the end-session URL to open, or null when the provider is unreachable or has none.
   */
  async signOut(): Promise<string | null> {
    this.#generation += 1
    this.#refreshing = null
    const refreshToken = await this.#store.getKV<string>(KEY_REFRESH)
    const idToken = await this.#store.getKV<string>(KEY_ID_TOKEN)
    const access = this.#access?.token ?? null
    await this.#clear()
    let provider: { clientId: string; provider: Provider }
    try {
      provider = await this.#provider()
    } catch {
      return null
    }
    const token = refreshToken ?? access
    if (token !== null) await this.#withinTime(revoke(provider.provider, provider.clientId, token, this.#fetch), REVOKE_WAIT_MS)
    return endSessionUrl(provider.provider, provider.clientId, `${this.#origin}/`, idToken)
  }

  onSignedOut(listener: () => void): () => void {
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
      this.#oidc = { clientId, provider: await discover(config.issuer, this.#fetch) }
    }
    return { ...this.#oidc, config }
  }

  async #save(tokens: Tokens): Promise<void> {
    this.#access = { token: tokens.access_token, expiresAt: tokens.expires_at }
    if (tokens.refresh_token !== null) await this.#store.setKV(KEY_REFRESH, tokens.refresh_token)
    else await this.#store.deleteKV(KEY_REFRESH)
    if (tokens.id_token !== null) await this.#store.setKV(KEY_ID_TOKEN, tokens.id_token)
    else await this.#store.deleteKV(KEY_ID_TOKEN)
  }

  /** Forgets the tokens and tells the listeners (once per sign-in). */
  async #clear(): Promise<void> {
    const wasSignedIn = this.#state === "signedIn"
    this.#access = null
    this.#state = "signedOut"
    await this.#store.deleteKV(KEY_REFRESH)
    await this.#store.deleteKV(KEY_ID_TOKEN)
    if (wasSignedIn) for (const listener of [...this.#listeners]) listener()
  }

  async #withinTime(work: Promise<void>, ms: number): Promise<void> {
    let timer: ReturnType<typeof setTimeout> | undefined
    const limit = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, ms)
    })
    try {
      await Promise.race([work.catch(() => {}), limit])
    } finally {
      clearTimeout(timer)
    }
  }

  /** The renewal, inside a Web Lock when there is one: two tabs must not send the same refresh token. */
  async #renew(): Promise<string> {
    const locks = (globalThis.navigator as Navigator | undefined)?.locks as LockManager | null | undefined
    if (locks === undefined || locks === null) return this.#renewLocked()
    return await locks.request(REFRESH_LOCK, () => this.#renewLocked())
  }

  async #renewLocked(): Promise<string> {
    const generation = this.#generation
    // Another tab may have renewed meanwhile, and the provider refuses the old token: always the stored one.
    let sent = await this.#store.getKV<string>(KEY_REFRESH)
    if (sent === null) return this.#endSession(generation)
    const { clientId, provider } = await this.#provider()
    let tokens: Tokens
    try {
      tokens = await refresh(provider, clientId, sent, this.#fetch, this.#now)
    } catch (error) {
      if (!(error instanceof OidcError) || error.kind !== "signed_out") throw error
      // Refused: if another tab stored a newer token in the meantime, that one gets a single try.
      const latest = await this.#store.getKV<string>(KEY_REFRESH)
      if (latest === null || latest === sent) return this.#endSession(generation)
      sent = latest
      try {
        tokens = await refresh(provider, clientId, sent, this.#fetch, this.#now)
      } catch (second) {
        if (second instanceof OidcError && second.kind === "signed_out") return this.#endSession(generation)
        throw second
      }
    }
    // A sign-in or sign-out finished while the request was out: this result is nobody's.
    if (generation !== this.#generation) {
      if (this.#state === "signedIn" && this.#access !== null) return this.#access.token // a new sign-in won
      throw new OidcError("signed_out", "You are signed out.")
    }
    // The ID token only serves as the end-session hint: keep the login's one when none comes back.
    if (tokens.id_token === null) tokens.id_token = await this.#store.getKV<string>(KEY_ID_TOKEN)
    await this.#save(tokens)
    return tokens.access_token
  }

  async #endSession(generation: number): Promise<never> {
    if (generation === this.#generation) {
      this.#generation += 1
      await this.#clear()
    }
    throw new OidcError("signed_out", "Your session ended. Sign in again.")
  }
}
