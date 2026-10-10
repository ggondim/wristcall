import { canonical, parseServerAddress, sameUrl } from "../lib/address"
import { ApiError, NetworkError, userMessage } from "../lib/errors"
import { ServerClient, type Health } from "../api/server"
import type { CloudServer } from "../api/cloud"
import type { SavedServer } from "../store/servers"
import {
  accountServerToken,
  ALREADY_ADDED,
  ALREADY_LINKED,
  BAD_ADDRESS,
  checkName,
  MALFORMED_REPLY,
  SIGN_IN_FIRST,
  type AccountLink,
  type ServersModel,
} from "./servers"

export type AddResult = { ok: true; server: SavedServer } | { ok: false; message: string }

export const NOT_A_PAT = "This is not a personal token: it starts with wc_pat_."
export const TOO_OLD = "This server is too old for this app. Update it."
export const TOKEN_REFUSED_ON_ADD = "This token was refused."
export const BAD_CODE_FORMAT = "Enter the 8-digit code."
export const CODE_REJECTED = "The code is wrong, expired or used."
export const TOO_MANY_ATTEMPTS = "Too many attempts. Wait a minute."
/** The code was spent and the server made a token for it, but the browser could not keep it. Never carries the token. */
export const NOT_SAVED =
  "Your account is linked, but this browser could not save the server. The personal token the server made for it " +
  'stays valid: on the server\'s host, find it with "wristcall users tokens list --user <you>" (named "account link") ' +
  'and revoke it with "wristcall users tokens revoke <id>". Then add the server again.'

const PAT_PREFIX = "wc_pat_"

/** Adds a server with a personal token, or with a pairing code made on its host (needs the account sign-in). */
export class AddServerModel {
  readonly #servers: ServersModel
  readonly #session: AccountLink | null
  readonly #origin: string
  readonly #clientFor: (url: URL, token: string | null) => ServerClient

  constructor(
    servers: ServersModel,
    session: AccountLink | null,
    opts: { origin?: string; addressClientFor?: (url: URL, token: string | null) => ServerClient } = {},
  ) {
    this.#servers = servers
    this.servers = servers
    this.#session = session
    this.#origin = opts.origin ?? globalThis.location.origin
    this.#clientFor = opts.addressClientFor ?? ((url, token) => new ServerClient(url, token))
  }

  readonly servers: ServersModel

  /**
   * The server of the account's agenda at this address, if any. A link may carry an address and a name to fill in:
   * only these are trusted, so a link from outside cannot steer a pasted token to some other host.
   */
  accountServer(url: string): CloudServer | null {
    return this.servers.state.fromAccount.find((entry) => sameUrl(entry.url, url)) ?? null
  }

  /** The pairing code is offered only with an account sign-in. */
  get canUseCode(): boolean {
    return this.#session !== null && this.#session.state === "signedIn"
  }

  async addWithToken(address: string, token: string, name = ""): Promise<AddResult> {
    const url = parseServerAddress(address)
    if (url === null) return fail(BAD_ADDRESS)
    const pat = token.trim()
    if (!pat.startsWith(PAT_PREFIX)) return fail(NOT_A_PAT) // a device token ends here too: it never has this prefix
    const clean = cleanName(name, url)
    if (typeof clean !== "string") return clean
    const client = this.#clientFor(url, pat)
    try {
      if (await this.#servers.has(canonical(url))) return fail(ALREADY_ADDED)
      await this.#health(client)
      try {
        await client.providers()
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) return fail(TOKEN_REFUSED_ON_ADD)
        if (e instanceof ApiError && e.status === 404) return fail(TOO_OLD)
        if (e instanceof ApiError && e.status === 403) return fail("This is a device token: use a personal token.")
        throw e
      }
      return await this.#save(url, clean, pat, false)
    } catch (e) {
      return fail(userMessage(e))
    }
  }

  async addWithCode(address: string, code: string, name = ""): Promise<AddResult> {
    const url = parseServerAddress(address)
    if (url === null) return fail(BAD_ADDRESS)
    const digits = code.replace(/\s+/g, "")
    if (!/^[0-9]{8}$/.test(digits)) return fail(BAD_CODE_FORMAT)
    const session = this.#session
    if (session === null || session.state !== "signedIn") return fail(SIGN_IN_FIRST)
    const clean = cleanName(name, url) // before the code is spent
    if (typeof clean !== "string") return clean
    const client = this.#clientFor(url, null)
    let serverToken: string
    try {
      // Before the code is spent: a duplicate would leave a token on the server that nobody keeps.
      if (await this.#servers.has(canonical(url))) return fail(ALREADY_ADDED)
      serverToken = await accountServerToken(url, await this.#health(client), session)
    } catch (e) {
      return fail(userMessage(e))
    }
    let apiToken: string
    try {
      const reply = await client.linkWithCode(serverToken, digits)
      if (reply.linked !== true || typeof reply.api_token !== "string" || !reply.api_token.startsWith(PAT_PREFIX)) {
        return fail(MALFORMED_REPLY)
      }
      apiToken = reply.api_token
    } catch (e) {
      return fail(linkMessage(e))
    }
    try {
      return await this.#save(url, clean, apiToken, true)
    } catch {
      return fail(NOT_SAVED)
    }
  }

  async #health(client: ServerClient): Promise<Health> {
    try {
      return await client.health()
    } catch (e) {
      if (e instanceof NetworkError) {
        // The browser does not say why a request failed. The usual cause is the server not allowing this site.
        throw new Error(
          `Can't reach this server from this site. If it is online, its owner must add ${this.#origin} to server.cors_origins.`,
        )
      }
      throw e
    }
  }

  async #save(url: URL, name: string, token: string, linked: boolean): Promise<AddResult> {
    const server = await this.#servers.add({
      name,
      url: canonical(url),
      token,
      linked,
      cloudServerId: null,
      pushKey: null,
      pushEndpoint: null,
    })
    return { ok: true, server }
  }
}

function fail(message: string): AddResult {
  return { ok: false, message }
}

/** The name to save (the host when none was typed), or the failure to show. */
function cleanName(name: string, url: URL): string | AddResult {
  const clean = name.trim()
  if (clean === "") return url.host
  try {
    checkName(clean)
  } catch (e) {
    return fail(userMessage(e))
  }
  return clean
}

function linkMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 409) return ALREADY_LINKED
    if (e.status === 429) return TOO_MANY_ATTEMPTS
    if (e.status === 401 && e.code === "invalid_code") return CODE_REJECTED
  }
  return userMessage(e)
}
