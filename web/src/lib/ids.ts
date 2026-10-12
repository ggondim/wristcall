/** A fresh random identifier for a local record. */
export function newId(): string {
  return crypto.randomUUID()
}

/** The four digits of a pairing request (`request_id`): the only shape the app puts in a path or acts on. */
export const REQUEST_ID = /^[0-9]{4}$/
