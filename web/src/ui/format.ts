import { unixNow } from "../lib/time"

const UNITS: { unit: Intl.RelativeTimeFormatUnit; seconds: number }[] = [
  { unit: "year", seconds: 365 * 86_400 },
  { unit: "month", seconds: 30 * 86_400 },
  { unit: "day", seconds: 86_400 },
  { unit: "hour", seconds: 3_600 },
  { unit: "minute", seconds: 60 },
]

/** "5 min. ago", "yesterday": a call's age from its Unix seconds. */
export function relativeTime(seconds: number | undefined, now: number = unixNow()): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return ""
  const age = now - seconds
  const format = new Intl.RelativeTimeFormat(undefined, { numeric: "auto", style: "short" })
  for (const { unit, seconds: size } of UNITS) {
    if (Math.abs(age) >= size) return format.format(-Math.trunc(age / size), unit)
  }
  return format.format(0, "second")
}

/** Date and time of a Unix seconds value, in the user's locale. */
export function dateTime(seconds: number | undefined): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return ""
  return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
}

export function dateOnly(seconds: number | undefined): string {
  if (seconds === undefined || !Number.isFinite(seconds)) return ""
  return new Date(seconds * 1000).toLocaleDateString(undefined, { dateStyle: "medium" })
}
