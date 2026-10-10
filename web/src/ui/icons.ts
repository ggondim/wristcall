// Agent icons. The server stores a plain name (an SF Symbol name on the iPhone, `^[a-z0-9]+(\.[a-z0-9]+)*$`); the
// browser has no SF Symbols, so each name of the picker maps to a drawing of our own. Drawings are data (path `d`
// strings and circles) that a component turns into `<svg>` elements: nothing here is markup parsed from a string at
// run time, and no icon font or CDN is involved.

export type Shape = { readonly d: string } | { readonly cx: number; readonly cy: number; readonly r: number }

/** The names of `IconPicker.symbols` in `watch/Phone/IconPicker.swift`, in the same order. */
export const ICONS: readonly string[] = [
  "waveform", "brain", "sparkles", "bubble.left", "text.bubble",
  "envelope", "calendar", "checklist", "note.text", "lightbulb",
  "house", "car", "briefcase", "cart", "heart",
  "book", "globe", "terminal", "wrench", "phone",
]

const BUBBLE = "M5 4h14a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-8l-4 4v-4H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1z"

const SHAPES: Record<string, readonly Shape[]> = {
  waveform: [{ d: "M4 10v4M8 7v10M12 4v16M16 8v8M20 10v4" }],
  brain: [
    { d: "M12 4.5C12 3.5 11 3 10 3C8.5 3 7.2 4 7 5.3C5.5 5.6 4.5 6.9 4.5 8.4C4.5 9.3 4.9 10.1 5.5 10.6C4.8 11.2 4.5 12 4.5 13C4.5 14.5 5.5 15.6 6.8 15.9C7 17.4 8.3 18.5 9.8 18.5C10.8 18.5 11.5 18.1 12 17.5Z" },
    { d: "M12 4.5C12 3.5 13 3 14 3C15.5 3 16.8 4 17 5.3C18.5 5.6 19.5 6.9 19.5 8.4C19.5 9.3 19.1 10.1 18.5 10.6C19.2 11.2 19.5 12 19.5 13C19.5 14.5 18.5 15.6 17.2 15.9C17 17.4 15.7 18.5 14.2 18.5C13.2 18.5 12.5 18.1 12 17.5Z" },
    { d: "M12 18v3" },
  ],
  sparkles: [
    { d: "M10 3l1.8 5.2L17 10l-5.2 1.8L10 17l-1.8-5.2L3 10l5.2-1.8z" },
    { d: "M18 14l.9 2.1L21 17l-2.1.9L18 20l-.9-2.1L15 17l2.1-.9z" },
  ],
  "bubble.left": [{ d: BUBBLE }],
  "text.bubble": [{ d: BUBBLE }, { d: "M8 8.5h8M8 12h5" }],
  envelope: [
    { d: "M5 5h14a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" },
    { d: "M4 7l8 6 8-6" },
  ],
  calendar: [
    { d: "M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" },
    { d: "M4 10h16M8 3v4M16 3v4" },
  ],
  checklist: [
    { d: "M11 7h9M11 13h9M11 19h9" },
    { d: "M4 7l1.5 1.5L8 5.5M4 13l1.5 1.5L8 11.5M4 19l1.5 1.5L8 17.5" },
  ],
  "note.text": [
    { d: "M6 3h9l4 4v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" },
    { d: "M14 3v5h5M8 12h8M8 16h6" },
  ],
  lightbulb: [
    { d: "M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z" },
    { d: "M9.5 19h5M10.5 21.5h3" },
  ],
  house: [{ d: "M3 11l9-7.5 9 7.5" }, { d: "M5.5 9.5V20h13V9.5" }, { d: "M10 20v-6h4v6" }],
  car: [
    { d: "M4 17v-5l2-5h12l2 5v5z" },
    { d: "M4 12h16" },
    { cx: 8, cy: 17.5, r: 1.7 },
    { cx: 16, cy: 17.5, r: 1.7 },
  ],
  briefcase: [
    { d: "M4 8h16a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z" },
    { d: "M9 8V6a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M3 13h18" },
  ],
  cart: [{ d: "M3 4h2.5l2.2 11h10.3l2-8H6.5" }, { cx: 9, cy: 19, r: 1.5 }, { cx: 17, cy: 19, r: 1.5 }],
  heart: [{ d: "M12 20s-8-4.9-8-10.5A4.5 4.5 0 0 1 12 7a4.5 4.5 0 0 1 8 2.5C20 15.1 12 20 12 20z" }],
  book: [{ d: "M12 6c-2-1.5-5-2-8-2v14c3 0 6 .5 8 2 2-1.5 5-2 8-2V4c-3 0-6 .5-8 2zM12 6v14" }],
  globe: [{ cx: 12, cy: 12, r: 9 }, { d: "M3 12h18M12 3c3.5 3 3.5 15 0 18M12 3c-3.5 3-3.5 15 0 18" }],
  terminal: [
    { d: "M4 5h16a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z" },
    { d: "M7 10l3 2-3 2M12 15h5" },
  ],
  wrench: [{ d: "M14.5 4a4.5 4.5 0 0 0-4.2 6.1L4.2 16.2a2 2 0 0 0 2.8 2.8l6.1-6.1A4.5 4.5 0 0 0 19.5 9l-2.8 2.8-2.5-.5-.5-2.5L16.5 6A4.5 4.5 0 0 0 14.5 4z" }],
  phone: [{ d: "M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a1 1 0 0 1-1 1A15 15 0 0 1 4 5a1 1 0 0 1 1-1z" }],
}

/** What a name the app does not know (one made on the iPhone, say) shows: a question mark in a circle. */
const GENERIC: readonly Shape[] = [
  { cx: 12, cy: 12, r: 9 },
  { d: "M9.6 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1.1.9-1.1 1.7" },
  { cx: 12, cy: 17, r: 0.4 },
]

/** The drawing of an icon name, or the generic one. `Object.hasOwn` keeps names such as `constructor` out. */
export function iconShapes(name: string): readonly Shape[] {
  return Object.hasOwn(SHAPES, name) ? SHAPES[name]! : GENERIC
}

/**
 * The same drawing as SVG text (viewBox `0 0 24 24`, only `<path>` and `<circle>`, no text nodes). Screens do not put
 * this into the page (they build `<svg>` from `iconShapes`); it is for tests and for anything that needs a file.
 */
export function iconSvg(name: string): string {
  const parts = iconShapes(name).map((shape) =>
    "d" in shape ? `<path d="${shape.d}"/>` : `<circle cx="${shape.cx}" cy="${shape.cy}" r="${shape.r}"/>`,
  )
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${parts.join("")}</svg>`
}
