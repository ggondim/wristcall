export type SaveResult = "done" | "needs-tap"

/** How long the object URL of a download lives: the browser needs it until the download has started. */
export const REVOKE_AFTER_MS = 10_000

/**
 * Hands a file to the user. Where the browser can share files (an iPhone Home Screen app, most phones) it opens the share
 * sheet: `<a download>` with a Blob often does nothing there. Sharing needs a recent tap, and the export waits for the
 * network first: when the browser refuses for that reason the result is `needs-tap`, and the caller offers a button that
 * calls this again (`afterTap`). Without file sharing, or when sharing fails otherwise, it downloads with an anchor.
 */
export async function saveFile(blob: Blob, filename: string, afterTap = false): Promise<SaveResult> {
  const file = new File([blob], filename, { type: blob.type })
  if (typeof navigator.share === "function" && navigator.canShare?.({ files: [file] })) {
    try {
      await navigator.share({ files: [file] })
      return "done"
    } catch (e) {
      const name = e instanceof Error ? e.name : ""
      if (name === "AbortError") return "done" // the user closed the sheet
      if (name === "NotAllowedError" && !afterTap) return "needs-tap"
      // Any other failure: the anchor below is the last resort.
    }
  }
  download(blob, filename)
  return "done"
}

function download(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  anchor.rel = "noopener"
  anchor.hidden = true
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
  setTimeout(() => URL.revokeObjectURL(url), REVOKE_AFTER_MS)
}
