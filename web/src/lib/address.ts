// Server and Cloud addresses: https:// with a host; http:// only for loopback (same rule as the Kit's ServerAddress).
const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1"]) // the CSP can name only these (no IPv6 literal host source)

/** The address as a URL, or null. Surrounding spaces and trailing slashes are dropped; no query, fragment or credentials. */
export function parseServerAddress(input: string): URL | null {
  let text = input.trim()
  while (text.endsWith("/")) text = text.slice(0, -1)
  if (text === "" || text.includes("?") || text.includes("#")) return null
  if (!/^https?:\/\/[^/]/i.test(text)) return null // "https:///x" would parse, as a host named x
  let url: URL
  try {
    url = new URL(text)
  } catch {
    return null
  }
  if (url.hostname === "" || url.username !== "" || url.password !== "") return null
  if (url.protocol === "https:") return url
  if (url.protocol === "http:" && LOOPBACK_HOSTS.has(url.hostname)) return url
  return null
}

/** Scheme and host lowercased, default port dropped, no trailing slash (path kept): the form used to compare addresses. */
export function canonical(url: URL): string {
  const path = url.pathname.replace(/\/+$/, "")
  return `${url.protocol}//${url.host}${path}`
}

export function sameUrl(a: string, b: string): boolean {
  const left = parseServerAddress(a)
  const right = parseServerAddress(b)
  return left !== null && right !== null && canonical(left) === canonical(right)
}
