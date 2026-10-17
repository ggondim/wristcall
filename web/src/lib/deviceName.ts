/** The Cloud's limit for a device name. */
const MAX_LENGTH = 64

/** Checked in order: Edge and the iOS browsers also name Chrome or Safari in their user agent. */
const BROWSERS: [RegExp, string][] = [
  [/\bEdg(e|A|iOS)?\//, "Edge"],
  [/\b(Firefox|FxiOS)\//, "Firefox"],
  [/\b(Chrome|CriOS|Chromium)\//, "Chrome"],
  [/\bVersion\/[\d.]+.*\bSafari\//, "Safari"],
]

/** Checked in order: Android and ChromeOS also say Linux, the iPhone says "like Mac OS X". */
const SYSTEMS: [RegExp, string][] = [
  [/\biPad\b/, "iPad"],
  [/\b(iPhone|iPod)\b/, "iPhone"],
  [/\bAndroid\b/, "Android"],
  [/\bCrOS\b/, "ChromeOS"],
  [/\bWindows\b/, "Windows"],
  [/\b(Macintosh|Mac OS X)\b/, "macOS"],
  [/\bLinux\b/, "Linux"],
]

function first(table: [RegExp, string][], userAgent: string): string | null {
  return table.find(([pattern]) => pattern.test(userAgent))?.[1] ?? null
}

/** The name of this browser as a Cloud device (`Chrome on macOS`), `Web browser` when it is not recognized. */
export function browserDeviceName(userAgent: string): string {
  const browser = first(BROWSERS, userAgent) ?? "Web browser"
  const system = first(SYSTEMS, userAgent)
  return (system === null ? browser : `${browser} on ${system}`).slice(0, MAX_LENGTH)
}
