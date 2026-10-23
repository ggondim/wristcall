import { canonical, parseServerAddress } from "../lib/address"
import { ApiError } from "../lib/errors"
import type { CloudRoute } from "./cloud"
import { apiUrl, ServerClient } from "./server"

/** What the routed client needs from the router (the real `CloudRouter`). */
export interface ManageRouter {
  manage(): Promise<CloudRoute>
  invalidate(): void
}

const BAD_NODE = "The Cloud's route was not understood."

/** Never asked: every request goes to the node of the route (`through` below). */
const NO_BASE = new URL("https://wristcall.invalid")

/**
 * The management client of the Wristcall Cloud entry: the same routes as a server's, on the node the Cloud routes the
 * account to, with that node's per-server token. The route is asked for on every request (the router keeps a `manage`
 * route until 30 s before it expires), so a client kept by a screen for longer than the token's 300 s keeps working.
 * Creating it reaches nothing and cannot fail: a Cloud with no node answers the first request (`503 no_node_available`).
 * A `401` from the node drops the route and the request runs once more with a new one. No client header goes to the node.
 */
export class RoutedServerClient extends ServerClient {
  readonly #router: ManageRouter

  constructor(router: ManageRouter, fetchImpl?: typeof fetch) {
    super(NO_BASE, null, fetchImpl)
    this.#router = router
  }

  protected override async through<T>(
    path: string,
    params: URLSearchParams | undefined,
    auth: boolean,
    go: (url: string, bearer: string | undefined) => Promise<T>,
  ): Promise<T> {
    for (let attempt = 1; ; attempt++) {
      const route = await this.#router.manage()
      const node = parseServerAddress(route.node.url)
      if (node === null) throw new ApiError(200, "bad_response", BAD_NODE)
      try {
        return await go(apiUrl(canonical(node), path, params), auth ? route.token : undefined)
      } catch (error) {
        if (attempt > 1 || !auth || !(error instanceof ApiError) || error.status !== 401) throw error
        this.#router.invalidate()
      }
    }
  }
}
