import { describe, expect, test } from "vitest"
import { ApiError } from "../lib/errors"
import { mockFetch } from "../test/mockFetch"
import { ServerClient } from "./server"

const BASE = new URL("https://s.example")
const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status })

describe("ServerClient", () => {
  test("server: ids are one path segment", async () => {
    const fetchMock = mockFetch(() => new Response(null, { status: 204 }))
    await new ServerClient(BASE, "wc_pat_x", fetchMock).deleteAgent("../devices")
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/agents/..%2Fdevices")
    expect(fetchMock.lastInit().method).toBe("DELETE")
  })

  test("server: dot segments are refused without calling", async () => {
    const fetchMock = mockFetch(() => ok({}))
    const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
    for (const bad of ["", ".", ".."]) await expect(client.agent(bad)).rejects.toThrow()
    await expect(client.call("..")).rejects.toThrow()
    expect(fetchMock.calls).toHaveLength(0)
  })

  test("server: approve refuses a malformed request id without calling", async () => {
    const fetchMock = mockFetch(() => new Response("{}"))
    const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
    await expect(client.approve("12a4")).rejects.toThrow()
    await expect(client.deny("../1234")).rejects.toThrow()
    expect(fetchMock.calls).toHaveLength(0)
    await client.approve("1234")
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/pairing-requests/1234/approve")
  })

  test("server: base path is kept and routes unwrap their lists", async () => {
    const fetchMock = mockFetch((url) =>
      url.endsWith("/agents") ? ok({ agents: [{ id: "a" }] }) : url.endsWith("/devices") ? ok({ devices: [{ id: "d" }] }) : ok({ requests: [] }),
    )
    const client = new ServerClient(new URL("https://s.example/wc/"), "wc_pat_x", fetchMock)
    expect(await client.agents()).toEqual([{ id: "a" }])
    expect(fetchMock.lastUrl()).toBe("https://s.example/wc/v1/agents")
    expect(await client.devices()).toEqual([{ id: "d" }])
    expect(await client.pairingRequests()).toEqual([])
  })

  test("server: health sends no token", async () => {
    const fetchMock = mockFetch(() => ok({ status: "ok", version: "0.7.0", protocol: 1, account: null, push: null }))
    await new ServerClient(BASE, null, fetchMock).health()
    expect((fetchMock.lastInit().headers as Record<string, string>).Authorization).toBeUndefined()
  })

  test("server: a route that needs a token refuses without one", async () => {
    const fetchMock = mockFetch(() => ok({}))
    const e = await new ServerClient(BASE, null, fetchMock).agents().catch((x) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(fetchMock.calls).toHaveLength(0)
  })

  test("server: link with a code sends no Authorization", async () => {
    const fetchMock = mockFetch(() => ok({ linked: true, issuer: "i", user: { id: "u", handle: "h" }, api_token: "wc_pat_new" }))
    await new ServerClient(BASE, null, fetchMock).linkWithCode("srv-token", "12345678")
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/account/link")
    expect((fetchMock.lastInit().headers as Record<string, string>).Authorization).toBeUndefined()
    expect(JSON.parse(fetchMock.lastInit().body as string)).toEqual({ token: "srv-token", code: "12345678" })
  })

  test("server: link with a token, push key and unlink use the personal token", async () => {
    const fetchMock = mockFetch(() => new Response(null, { status: 204 }))
    const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
    await client.setPushKey("wc_push_k")
    expect([fetchMock.lastInit().method, fetchMock.lastUrl()]).toEqual(["PUT", "https://s.example/v1/push"])
    expect(JSON.parse(fetchMock.lastInit().body as string)).toEqual({ push_key: "wc_push_k" })
    await client.clearPushKey()
    expect(fetchMock.lastInit().method).toBe("DELETE")
    await client.unlink()
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/account/link")
    await client.linkWithToken("srv-token").catch(() => {})
    expect((fetchMock.lastInit().headers as Record<string, string>).Authorization).toBe("Bearer wc_pat_x")
  })

  test("serverToJsonHasNoToken", () => {
    const client = new ServerClient(BASE, "wc_pat_secret")
    expect(JSON.stringify(client)).not.toContain("wc_pat_secret")
    expect(String(Object.getOwnPropertyNames(client)) + String(Object.values(client))).not.toContain("wc_pat_secret")
  })

  test("callsQueryEncoding", async () => {
    const fetchMock = mockFetch(() => ok({ calls: [], next_before: null }))
    await new ServerClient(BASE, "wc_pat_x", fetchMock).calls({ q: " reunião & café ", agent: "a b", since: 10.9, limit: 500, before: "c_1" })
    const url = new URL(fetchMock.lastUrl())
    expect(url.pathname).toBe("/v1/calls")
    expect(url.searchParams.get("q")).toBe("reunião & café")
    expect(fetchMock.lastUrl()).toContain("q=reuni%C3%A3o+%26+caf%C3%A9")
    expect(url.searchParams.get("agent")).toBe("a b")
    expect(url.searchParams.get("since")).toBe("10")
    expect(url.searchParams.get("limit")).toBe("100")
    expect(url.searchParams.get("before")).toBe("c_1")
  })

  test("callsQueryWorksWithoutUrlSearchParamsSize", async () => {
    // Safari before 17 has no URLSearchParams.size: the query string must not depend on it.
    const proto = URLSearchParams.prototype
    const saved = Object.getOwnPropertyDescriptor(proto, "size")
    delete (proto as { size?: number }).size
    try {
      expect("size" in new URLSearchParams("a=1")).toBe(false)
      const fetchMock = mockFetch(() => ok({ calls: [], next_before: null }))
      const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
      await client.calls({ agent: "notes", before: "c_1" })
      expect(new URL(fetchMock.lastUrl()).searchParams.get("agent")).toBe("notes")
      expect(new URL(fetchMock.lastUrl()).searchParams.get("limit")).toBe("50")
      await client.exportCalls("json", {})
      expect(new URL(fetchMock.lastUrl()).searchParams.get("format")).toBe("json")
      await client.agents()
      expect(fetchMock.lastUrl()).toBe("https://s.example/v1/agents")
    } finally {
      if (saved) Object.defineProperty(proto, "size", saved)
    }
  })

  test("callsLimitFallsBackWhenNotFinite", async () => {
    const fetchMock = mockFetch(() => ok({ calls: [], next_before: null }))
    const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
    for (const limit of [NaN, Infinity]) {
      await client.calls({ limit })
      const wanted = new URL(fetchMock.lastUrl()).searchParams.get("limit")
      expect(wanted).toBe("50")
    }
    await client.calls({ limit: 0 })
    expect(new URL(fetchMock.lastUrl()).searchParams.get("limit")).toBe("1")
  })

  test("callsQueryCutsTextByCharacters", async () => {
    const fetchMock = mockFetch(() => ok({ calls: [], next_before: null }))
    await new ServerClient(BASE, "wc_pat_x", fetchMock).calls({ q: "😀".repeat(501) })
    const q = new URL(fetchMock.lastUrl()).searchParams.get("q") ?? ""
    expect(Array.from(q)).toHaveLength(500)
    expect(q).toBe("😀".repeat(500))
  })

  test("deleteCallsSendsAgentOrAll", async () => {
    const fetchMock = mockFetch(() => ok({ deleted: 3 }))
    const client = new ServerClient(BASE, "wc_pat_x", fetchMock)
    expect(await client.deleteCalls({ agent: " notes " })).toBe(3)
    expect(fetchMock.lastInit().method).toBe("DELETE")
    let url = new URL(fetchMock.lastUrl())
    expect([url.pathname, url.searchParams.get("agent"), url.searchParams.has("all")]).toEqual(["/v1/calls", "notes", false])
    expect(await client.deleteCalls({ all: true })).toBe(3)
    url = new URL(fetchMock.lastUrl())
    expect([url.searchParams.get("all"), url.searchParams.has("agent")]).toEqual(["true", false])
  })

  test("deleteCallsRefusesBlankAgentWithoutCalling", async () => {
    const fetchMock = mockFetch(() => ok({ deleted: 9 }))
    await expect(new ServerClient(BASE, "wc_pat_x", fetchMock).deleteCalls({ agent: "  " })).rejects.toThrow("Choose an agent.")
    expect(fetchMock.calls).toHaveLength(0)
  })

  test("exportReadsFilename", async () => {
    const withHeader = mockFetch(
      () => new Response("# calls", { headers: { "Content-Disposition": 'attachment; filename="wristcall-calls.md"' } }),
    )
    const named = await new ServerClient(BASE, "wc_pat_x", withHeader).exportCalls("md", { agent: "notes" })
    expect(named.filename).toBe("wristcall-calls.md")
    expect(await named.blob.text()).toBe("# calls")
    const url = new URL(withHeader.lastUrl())
    expect([url.pathname, url.searchParams.get("format"), url.searchParams.get("agent")]).toEqual(["/v1/calls/export", "md", "notes"])

    const bare = mockFetch(() => new Response("{}"))
    expect((await new ServerClient(BASE, "wc_pat_x", bare).exportCalls("json", {})).filename).toBe("wristcall-calls.json")

    const hostile = mockFetch(() => new Response("x", { headers: { "Content-Disposition": 'attachment; filename="../../.ssh/id rsa"' } }))
    expect((await new ServerClient(BASE, "wc_pat_x", hostile).exportCalls("md", {})).filename).toBe("idrsa")
  })

  test("server: error bodies become ApiError", async () => {
    const fetchMock = mockFetch(() => ok({ error: "not_failed", message: "Only failed calls." }, 409))
    const e = await new ServerClient(BASE, "wc_pat_x", fetchMock).redeliver("c_1").catch((x) => x)
    expect([e.status, e.code, e.message]).toEqual([409, "not_failed", "Only failed calls."])
    expect(fetchMock.lastUrl()).toBe("https://s.example/v1/calls/c_1/redeliver")
  })
})
