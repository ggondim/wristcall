export type MockFetch = typeof fetch & {
  calls: { url: string; init: RequestInit }[]
  lastUrl(): string
  lastInit(): RequestInit
}

/** A `fetch` that answers from `handler` and records what it was asked. */
export function mockFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>): MockFetch {
  const calls: { url: string; init: RequestInit }[] = []
  const fn = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url
    calls.push({ url, init })
    return handler(url, init)
  }) as MockFetch
  fn.calls = calls
  fn.lastUrl = () => calls[calls.length - 1]!.url
  fn.lastInit = () => calls[calls.length - 1]!.init
  return fn
}
