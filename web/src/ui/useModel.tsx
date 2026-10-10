import { useEffect, useState } from "preact/hooks"

export interface Model<S> {
  readonly state: S
  subscribe(listener: () => void): () => void
}

/** Re-renders the component whenever the model's state changes. */
export function useModel<S>(model: Model<S>): S {
  const [, setTick] = useState(0)
  const rerender = () => setTick((n) => n + 1)
  useEffect(() => {
    const off = model.subscribe(rerender)
    rerender() // a change between the render and the subscription
    return off
  }, [model])
  return model.state
}

/** State holder for the screens' models: `set` swaps in a new state object and tells the listeners. */
export class BaseModel<S> implements Model<S> {
  #state: S
  readonly #listeners = new Set<() => void>()

  protected constructor(initial: S) {
    this.#state = initial
  }

  get state(): S {
    return this.#state
  }

  protected set(patch: Partial<S>): void {
    this.#state = { ...this.#state, ...patch }
    for (const listener of [...this.#listeners]) listener()
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }
}
