import { sameUrl } from "../lib/address"
import type { RelayClient } from "../api/relay"
import type { ServerClient } from "../api/server"
import type { SavedServer, ServerStore } from "../store/servers"

/** What the sync needs from the store (the real `ServerStore`, or a stand-in in a test). */
export type PushStore = Pick<ServerStore, "list" | "get" | "update">
export type PushRelay = Pick<RelayClient, "register" | "check" | "unregister">

/** One lock for the push keys of all tabs: two tabs must not register the same server twice. */
export const PUSH_LOCK = "wc.push"

/**
 * The browser's side of the device approval push (same rules as the iPhone's `PhonePushCoordinator`): this app's
 * own Cloud is the relay, never one a server names. For each server that has an account and names that Cloud as
 * its relay, the subscription is registered there (one key per server) and the key goes to the server with
 * `PUT /v1/push`. Every opening and return to the foreground checks the keys again: a key the relay forgot is
 * registered again, and a new subscription replaces the old keys. A server that cannot be reached, or does not
 * push, is skipped without a word on the screen.
 */
export class PushSync {
  readonly #store: PushStore
  readonly #relay: PushRelay
  readonly #clientFor: (server: SavedServer) => ServerClient
  readonly #cloudUrl: string
  #chain: Promise<void> = Promise.resolve()
  #stopped = false

  constructor(store: PushStore, relay: PushRelay, clientFor: (server: SavedServer) => ServerClient, cloudUrl: string) {
    this.#store = store
    this.#relay = relay
    this.#clientFor = clientFor
    this.#cloudUrl = cloudUrl
  }

  /** Brings every server in line with `subscription`. Without one (not asked yet, or cancelled) it does nothing. */
  syncAll(subscription: PushSubscription | null): Promise<void> {
    if (subscription === null) return Promise.resolve()
    return this.#exclusive(async () => {
      if (this.#stopped) return
      let servers: SavedServer[]
      try {
        servers = await this.#store.list()
      } catch {
        return
      }
      const json = subscription.toJSON()
      for (const server of servers) {
        if (this.#stopped) return
        try {
          await this.#sync(server, json, subscription.endpoint)
        } catch {
          // This server is skipped (down, push off, refused); the next opening tries again. Nothing to show.
        }
      }
    })
  }

  /**
   * A server leaving the app: `DELETE /v1/push` with its token, then the key dropped at the relay. Best effort,
   * each part on its own. `serverCleared` says the caller already deleted the key at the server.
   */
  forget(server: SavedServer, options: { serverCleared?: boolean } = {}): Promise<void> {
    return this.#exclusive(() => this.#forget(server, options.serverCleared === true))
  }

  /** The account was deleted: every key undone, and nothing registered again until the page is opened anew. */
  accountDeleted(): Promise<void> {
    this.#stopped = true
    return this.#exclusive(async () => {
      let servers: SavedServer[]
      try {
        servers = await this.#store.list()
      } catch {
        return
      }
      for (const server of servers) await this.#forget(server, false)
    })
  }

  async #forget(server: SavedServer, serverCleared: boolean): Promise<void> {
    const key = server.pushKey
    if (key === null) return
    if (!serverCleared) await this.#clientFor(server).clearPushKey().catch(() => {})
    await this.#relay.unregister(key).catch(() => {})
    // The server may be gone from the store already (removal): then there is nothing to clear.
    await this.#store.update(server.id, { pushKey: null, pushEndpoint: null }).catch(() => null)
  }

  async #sync(listed: SavedServer, subscription: PushSubscriptionJSON, endpoint: string): Promise<void> {
    if (listed.token === "") return
    const health = await this.#clientFor(listed).health()
    // Approvals exist only with the account, and the relay must be this app's Cloud: a server naming another one
    // would get this browser's subscription there.
    if (health.account == null || health.push == null || !sameUrl(health.push.relay, this.#cloudUrl)) return
    // The store is read again: the server may have been removed, or another tab may have registered it meanwhile.
    const server = await this.#store.get(listed.id)
    if (server === null) return
    if (server.pushKey !== null && server.pushEndpoint === endpoint) {
      if ((await this.#relay.check(server.pushKey)) === "ok") return
      await this.#register(server, subscription, endpoint, null)
      return
    }
    // No key yet, or a new subscription: the old key would push to a dead endpoint.
    await this.#register(server, subscription, endpoint, server.pushKey)
  }

  async #register(server: SavedServer, subscription: PushSubscriptionJSON, endpoint: string, old: string | null): Promise<void> {
    if (old !== null) await this.#relay.unregister(old).catch(() => {})
    const label = server.name.trim() === "" ? new URL(server.url).host : server.name.trim()
    const { push_key: key } = await this.#relay.register(subscription, label, server.id)
    // The server may have been removed while the relay answered.
    if (this.#stopped || (await this.#store.get(server.id)) === null) {
      await this.#relay.unregister(key).catch(() => {})
      return
    }
    try {
      await this.#clientFor(server).setPushKey(key)
    } catch (error) {
      // Not stored anywhere: dropped, or the next opening would register yet another key.
      await this.#relay.unregister(key).catch(() => {})
      throw error
    }
    const saved = await this.#store.update(server.id, { pushKey: key, pushEndpoint: endpoint })
    if (saved === null) await this.#forget({ ...server, pushKey: key }, false)
  }

  /** One step at a time inside this page, and one page at a time (Web Lock) across tabs. */
  #exclusive(work: () => Promise<void>): Promise<void> {
    const run = this.#chain.then(() => withLock(work))
    this.#chain = run.catch(() => {})
    return run
  }
}

async function withLock(work: () => Promise<void>): Promise<void> {
  const locks = (globalThis.navigator as Navigator | undefined)?.locks as LockManager | null | undefined
  if (locks === undefined || locks === null) return work()
  await locks.request(PUSH_LOCK, work)
}
