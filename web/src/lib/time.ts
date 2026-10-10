/** Now in Unix seconds, the unit of every `expires_at` the servers send. Every model's `now` returns this unit. */
export function unixNow(): number {
  return Date.now() / 1000
}
