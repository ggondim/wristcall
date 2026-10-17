import { encode } from "uqr"
import { useMemo } from "preact/hooks"

/** Dark modules as one path, a rectangle per horizontal run (`M x y h n v1 h -n z`). */
function modulesPath(data: boolean[][]): string {
  const parts: string[] = []
  data.forEach((row, y) => {
    let x = 0
    while (x < row.length) {
      if (!row[x]) {
        x += 1
        continue
      }
      const start = x
      while (x < row.length && row[x]) x += 1
      parts.push(`M${start} ${y}h${x - start}v1h-${x - start}z`)
    }
  })
  return parts.join("")
}

/**
 * A QR code drawn in JSX from the `uqr` matrix: no markup string, no `style` (the CSP has `style-src 'self'`).
 * Four modules of quiet zone; colors come from the `.qr-light`/`.qr-dark` classes.
 */
export function QrCode({ value, label }: { value: string; label: string }) {
  const { size, d } = useMemo(() => {
    const qr = encode(value, { ecc: "M", border: 4 })
    return { size: qr.size, d: modulesPath(qr.data) }
  }, [value])
  return (
    <svg class="qr" viewBox={`0 0 ${size} ${size}`} role="img" aria-label={label} shape-rendering="crispEdges">
      <rect class="qr-light" width={size} height={size} />
      <path class="qr-dark" d={d} />
    </svg>
  )
}
