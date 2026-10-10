import { cleanup } from "@testing-library/preact"
import { afterEach } from "vitest"

// Vitest runs without globals, so the library cannot register its own cleanup.
afterEach(() => cleanup())
