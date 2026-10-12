import { act, render } from "@testing-library/preact"
import { describe, expect, test, vi } from "vitest"
import { BaseModel, useModel } from "./useModel"

class Counter extends BaseModel<{ n: number; label: string }> {
  constructor() {
    super({ n: 0, label: "x" })
  }
  bump() {
    this.set({ n: this.state.n + 1 })
  }
}

function View({ model }: { model: Counter }) {
  const s = useModel(model)
  return <p>{`${s.label}:${s.n}`}</p>
}

describe("useModel", () => {
  test("model: set replaces the state object and tells listeners", () => {
    const m = new Counter()
    const before = m.state
    const listener = vi.fn()
    m.subscribe(listener)
    m.bump()
    expect(m.state).not.toBe(before)
    expect(m.state).toEqual({ n: 1, label: "x" })
    expect(before.n).toBe(0)
    expect(listener).toHaveBeenCalledTimes(1)
  })

  test("model: unsubscribe stops notifications", () => {
    const m = new Counter()
    const listener = vi.fn()
    const off = m.subscribe(listener)
    off()
    m.bump()
    expect(listener).not.toHaveBeenCalled()
  })

  test("model: a listener may unsubscribe while being notified", () => {
    const m = new Counter()
    const second = vi.fn()
    const off = m.subscribe(() => off())
    m.subscribe(second)
    m.bump()
    expect(second).toHaveBeenCalledTimes(1)
  })

  test("useModel: rerenders on change and unsubscribes on unmount", () => {
    const m = new Counter()
    const { container, unmount } = render(<View model={m} />)
    expect(container.textContent).toBe("x:0")
    act(() => m.bump())
    expect(container.textContent).toBe("x:1")
    unmount()
    expect(() => m.bump()).not.toThrow()
  })
})
