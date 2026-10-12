import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/preact"
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest"
import { ServerClient } from "../api/server"
import { ApprovalsModel } from "../models/approvals"
import { DevicesModel } from "../models/devices"
import { json, savedServer, serversWith, type Handler } from "../test/fakeServers"
import { mockFetch } from "../test/mockFetch"
import { App } from "./App"
import { ApprovalsView } from "./ApprovalsView"
import { DevicesScreen } from "./DevicesView"
import { PairingCodeView } from "./PairingCodeView"
import { BaseModel } from "./useModel"
import { parseApprovalTarget, parseDevicesTarget, parseRoute } from "./route"

const home = savedServer({ id: "home", name: "home", url: "https://home.example.com" })
const request = (request_id: string, expires_at: number, device_name = "Wrist") => ({ request_id, device_name, expires_at })
const farFuture = Date.now() / 1000 + 3600

function fakeHome(over: { devices?: object[]; requests?: object[]; onPost?: (path: string) => Response | null } = {}): Handler {
  const devices = [...(over.devices ?? [{ id: "d1", name: "Kitchen watch", created_at: 1_700_000_000 }])]
  return (path, method) => {
    if (method === "POST") return over.onPost?.(path) ?? new Response(null, { status: 204 })
    if (method === "DELETE") {
      devices.splice(0, devices.length, ...devices.filter((device) => `devices/${(device as { id: string }).id}` !== path))
      return new Response(null, { status: 204 })
    }
    if (path === "devices") return json({ devices })
    if (path === "pairing-requests") return json({ requests: over.requests ?? [] })
    return null
  }
}

class Badge extends BaseModel<{ count: number }> {
  constructor() {
    super({ count: 0 })
  }
}

beforeEach(() => history.replaceState(null, "", "/"))
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  history.replaceState(null, "", "/")
})

describe("routes", () => {
  test("devicesAndApprovalsAddressesParse", () => {
    expect(parseDevicesTarget("/servers/a%2Fb/devices")).toEqual({ serverId: "a/b", screen: "list" })
    expect(parseDevicesTarget("/servers/s1/devices/code")).toEqual({ serverId: "s1", screen: "code" })
    expect(parseDevicesTarget("/servers/s1/devices/x")).toBeNull()
    expect(parseDevicesTarget("/servers/s1/agents")).toBeNull()
    expect(parseRoute("/approvals")).toEqual({ tab: "approvals", path: "/approvals" })
    expect(parseApprovalTarget("?server=home&request=0423&expires=1760000000")).toEqual({ serverId: "home", requestId: "0423", expiresAt: 1760000000 })
    expect(parseApprovalTarget("?server=home&request=0423")).toEqual({ serverId: "home", requestId: "0423", expiresAt: null })
    expect(parseApprovalTarget("?server=home&request=0423&expires=soon")?.expiresAt).toBeNull()
    expect(parseApprovalTarget("?server=home")).toBeNull()
    expect(parseApprovalTarget("")).toBeNull()
  })
})

describe("DevicesScreen", () => {
  test("listsDevicesAndRevokesOneAfterAsking", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome() })
    render(<DevicesScreen servers={servers} approvals={new ApprovalsModel(servers)} target={{ serverId: "home", screen: "list" }} />)
    await screen.findByText("Kitchen watch")
    fireEvent.click(screen.getByRole("button", { name: "Revoke Kitchen watch" }))
    await screen.findByText(/The watch stops working with this server until it is paired again/)
    expect(fetchMock.calls.some((call) => call.init.method === "DELETE")).toBe(false)
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    expect(screen.queryByText(/The watch stops working/)).toBeNull()
    fireEvent.click(screen.getByRole("button", { name: "Revoke Kitchen watch" }))
    fireEvent.click(screen.getAllByRole("button", { name: "Revoke" })[0]!)
    await waitFor(() => expect(screen.queryByText("Kitchen watch")).toBeNull())
    expect(screen.getByText("No watches paired yet.")).toBeTruthy()
    expect(fetchMock.calls.find((call) => call.init.method === "DELETE")!.url).toBe("https://home.example.com/v1/devices/d1")
  })

  test("showsTheWaitingRequestsOfThisServerAndAnswersThem", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome({ requests: [request("0423", farFuture, "New watch")] }) })
    render(<DevicesScreen servers={servers} approvals={new ApprovalsModel(servers)} target={{ serverId: "home", screen: "list" }} />)
    await screen.findByText("New watch")
    fireEvent.click(screen.getByRole("button", { name: "Approve" }))
    await screen.findByText(/Approve New watch\? It will be able to call your agents on home until you revoke it\./)
    expect(fetchMock.calls.some((call) => call.init.method === "POST")).toBe(false)
    fireEvent.click(screen.getByRole("button", { name: "Approve New watch" }))
    await waitFor(() => expect(screen.queryByText("New watch")).toBeNull())
    expect(fetchMock.calls.find((call) => call.init.method === "POST")!.url).toBe("https://home.example.com/v1/pairing-requests/0423/approve")
  })

  test("approveNeedsAConfirmationAndCancelSendsNothing", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome({ requests: [request("0423", farFuture, "New watch")] }) })
    render(<ApprovalsView model={new ApprovalsModel(servers)} />)
    await screen.findByText("New watch")
    fireEvent.click(screen.getByRole("button", { name: "Approve" }))
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }))
    expect(screen.queryByText(/It will be able to call your agents/)).toBeNull()
    expect(screen.getByRole("button", { name: "Approve" })).toBeTruthy()
    expect(fetchMock.calls.some((call) => call.init.method === "POST")).toBe(false)
  })

  test("denyIsOneTap", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome({ requests: [request("0423", farFuture, "New watch")] }) })
    render(<ApprovalsView model={new ApprovalsModel(servers)} />)
    await screen.findByText("New watch")
    fireEvent.click(screen.getByRole("button", { name: "Deny" }))
    await waitFor(() => expect(screen.queryByText("New watch")).toBeNull())
    expect(fetchMock.calls.find((call) => call.init.method === "POST")!.url).toBe("https://home.example.com/v1/pairing-requests/0423/deny")
  })

  test("theDevicesAreReadOncePerOpen", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome() })
    render(<DevicesScreen servers={servers} approvals={new ApprovalsModel(servers)} target={{ serverId: "home", screen: "list" }} />)
    await screen.findByText("Kitchen watch")
    expect(fetchMock.calls.filter((call) => call.url.endsWith("/v1/devices"))).toHaveLength(1)
  })

  test("aRemovedServerSaysSo", async () => {
    const { servers } = serversWith([home])
    render(<DevicesScreen servers={servers} approvals={new ApprovalsModel(servers)} target={{ serverId: "gone", screen: "list" }} />)
    await screen.findByText("Server removed")
  })

  test("linksToThePairingCodeScreen", async () => {
    const { servers } = serversWith([home], { "home.example.com": fakeHome() })
    render(<DevicesScreen servers={servers} approvals={new ApprovalsModel(servers)} target={{ serverId: "home", screen: "list" }} />)
    expect((await screen.findByRole("link", { name: "Pairing code" })).getAttribute("href")).toBe("/servers/home/devices/code")
  })

  test("refreshesApprovalsEvery10SecondsWhileOpenAndStopsOnLeaving", async () => {
    const { servers } = serversWith([home], { "home.example.com": fakeHome() })
    const approvals = new ApprovalsModel(servers)
    const refresh = vi.spyOn(approvals, "refresh")
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const view = render(<DevicesScreen servers={servers} approvals={approvals} target={{ serverId: "home", screen: "list" }} />)
    await screen.findByText("Kitchen watch")
    const opened = refresh.mock.calls.length
    expect(opened).toBeGreaterThanOrEqual(1)
    act(() => void vi.advanceTimersByTime(10_000))
    expect(refresh.mock.calls.length).toBe(opened + 1)
    act(() => void vi.advanceTimersByTime(20_000))
    expect(refresh.mock.calls.length).toBe(opened + 3)
    act(() => void view.unmount())
    act(() => void vi.advanceTimersByTime(60_000))
    expect(refresh.mock.calls.length).toBe(opened + 3)
  })
})

describe("PairingCodeView", () => {
  function codeModel(now: () => number, over: object = {}) {
    const fetchMock = mockFetch(() => json({ code: "12345678", expires_at: 1_000, server_url: "https://home.example.com", via_directory: false, ...over }))
    return new DevicesModel(new ServerClient(new URL("https://home.example.com"), "wc_pat_x", fetchMock), now)
  }

  test("showsTheGroupedCodeTheAddressTheCountdownAndTheWatchInstruction", async () => {
    const model = codeModel(() => 500, { warning: "The directory is unreachable." })
    await model.newPairingCode()
    render(<PairingCodeView model={model} back="/servers/home/devices" />)
    expect(screen.getByText("1234 5678")).toBeTruthy()
    expect(screen.getByText("https://home.example.com")).toBeTruthy()
    expect(screen.getByText("8:20")).toBeTruthy()
    expect(screen.getByText("The directory is unreachable.")).toBeTruthy()
    expect(screen.getByText("On the watch: Add server, then type this code")).toBeTruthy()
    expect(screen.queryByText(/Add to watch/i)).toBeNull()
  })

  test("theCodeLeavesTheScreenWhenItExpires", async () => {
    let now = 998
    const model = codeModel(() => now)
    await model.newPairingCode()
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    render(<PairingCodeView model={model} back="/servers/home/devices" />)
    expect(screen.getByText("0:02")).toBeTruthy()
    now = 999
    act(() => void vi.advanceTimersByTime(1000))
    expect(screen.getByText("0:01")).toBeTruthy()
    now = 1_000
    act(() => void vi.advanceTimersByTime(1000))
    expect(screen.queryByText("1234 5678")).toBeNull()
    expect(screen.getByText("No code")).toBeTruthy()
  })

  test("opensWithAFreshCodeAndAsksForAnotherOnRequest", async () => {
    const model = codeModel(() => 0)
    render(<PairingCodeView model={model} back="/servers/home/devices" />)
    await screen.findByText("1234 5678")
    fireEvent.click(screen.getByRole("button", { name: "New code" }))
    await waitFor(() => expect(screen.getByText("1234 5678")).toBeTruthy())
  })

  test("showsTheServersReasonWhenNoCodeComes", async () => {
    const fetchMock = mockFetch(() => json({ error: "limit", message: "Device limit reached (5)." }, 403))
    const model = new DevicesModel(new ServerClient(new URL("https://home.example.com"), "wc_pat_x", fetchMock), () => 0)
    render(<PairingCodeView model={model} back="/servers/home/devices" />)
    expect((await screen.findByRole("alert")).textContent).toBe("Device limit reached (5).")
    expect(screen.getByRole("button", { name: "Make a code" })).toBeTruthy()
  })
})

describe("ApprovalsView and the App", () => {
  test("listsTheRequestsOfAllServersWithDeviceServerAndTimeLeft", async () => {
    const office = savedServer({ id: "office", name: "office", url: "https://office.example.com" })
    const { servers } = serversWith([home, office], {
      "home.example.com": fakeHome({ requests: [request("0423", farFuture, "Wrist A")] }),
      "office.example.com": fakeHome({ requests: [request("0777", farFuture + 100, "Wrist B")] }),
    })
    render(<ApprovalsView model={new ApprovalsModel(servers)} />)
    await screen.findByText("Wrist A")
    expect(screen.getByText("Wrist B")).toBeTruthy()
    expect(screen.getByText("home")).toBeTruthy()
    expect(screen.getByText("office")).toBeTruthy()
    expect(screen.getAllByText(/^Expires in \d+:\d\d$/)).toHaveLength(2)
    expect(screen.getAllByRole("button", { name: "Deny" })).toHaveLength(2)
  })

  test("showsNothingToApproveAndKeepsPollingUntilItLeaves", async () => {
    const { servers } = serversWith([home], { "home.example.com": fakeHome() })
    const approvals = new ApprovalsModel(servers)
    const refresh = vi.spyOn(approvals, "refresh")
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] })
    const view = render(<ApprovalsView model={approvals} />)
    expect(screen.getByText("Nothing to approve")).toBeTruthy()
    const opened = refresh.mock.calls.length
    act(() => void vi.advanceTimersByTime(10_000))
    expect(refresh.mock.calls.length).toBe(opened + 1)
    act(() => void view.unmount())
    act(() => void vi.advanceTimersByTime(30_000))
    expect(refresh.mock.calls.length).toBe(opened + 1)
  })

  test("appCallsFocusWhenTheApprovalsRouteIsOpenedByANotificationAndHighlightsIt", async () => {
    const { servers } = serversWith([home], { "home.example.com": fakeHome({ requests: [request("0423", farFuture, "Wrist A"), request("0777", farFuture + 5, "Wrist B")] }) })
    const approvals = new ApprovalsModel(servers)
    const focus = vi.spyOn(approvals, "focus")
    history.replaceState(null, "", `/approvals?server=home&request=0423&expires=${farFuture}`)
    render(<App approvals={approvals} badge={new Badge()} servers={servers} />)
    await screen.findByText("Wrist A")
    expect(focus).toHaveBeenCalledWith("home", "0423", farFuture)
    await waitFor(() => expect(screen.getByText("Wrist A").closest("li")?.getAttribute("aria-current")).toBe("true"))
    // The address is read once: a relaunch must not focus the request again.
    expect(location.pathname + location.search).toBe("/approvals")
    expect(screen.getByText("Wrist B").closest("li")?.getAttribute("aria-current")).toBeNull()
  })

  test("appShowsTheExpiredNoticeForAPushWhoseRequestIsGone", async () => {
    const { servers } = serversWith([home], { "home.example.com": fakeHome({ requests: [request("0423", farFuture, "Wrist A")] }) })
    history.replaceState(null, "", `/approvals?server=home&request=0423&expires=${farFuture - 500}`)
    render(<App approvals={new ApprovalsModel(servers)} badge={new Badge()} servers={servers} />)
    expect((await screen.findByRole("alert")).textContent).toContain("This request expired.")
    fireEvent.click(screen.getByRole("button", { name: "OK" }))
    expect(screen.queryByRole("alert")).toBeNull()
  })

  test("appIgnoresAnApprovalsAddressWithoutAServerAndRequest", async () => {
    const { servers, fetchMock } = serversWith([home], { "home.example.com": fakeHome() })
    history.replaceState(null, "", "/approvals?request=0423")
    const approvals = new ApprovalsModel(servers)
    const focus = vi.spyOn(approvals, "focus")
    render(<App approvals={approvals} badge={new Badge()} servers={servers} />)
    await screen.findByText("Nothing to approve")
    expect(focus).not.toHaveBeenCalled()
    expect(fetchMock.calls.every((call) => !call.init.method || call.init.method === "GET")).toBe(true)
  })

  test("theBadgeOpensTheApprovalsScreen", () => {
    const { servers } = serversWith([home])
    const badge = new Badge()
    // @ts-expect-error: the test sets the count through the protected setter
    badge.set({ count: 2 })
    render(<App approvals={new ApprovalsModel(servers)} badge={badge} servers={servers} />)
    const link = screen.getByRole("link", { name: "2 pending approvals" })
    expect(link.getAttribute("href")).toBe("/approvals")
    fireEvent.click(link)
    expect(location.pathname).toBe("/approvals")
    expect(screen.getByRole("heading", { name: "Approvals" })).toBeTruthy()
    expect(screen.getByRole("link", { name: "Servers" }).getAttribute("aria-current")).toBe("page")
  })
})
