import Foundation
import Synchronization
import Testing
import WristcallKit
@testable import WristcallPhone

/// Plays `WCSession`: the state is set by the test, handlers run on `DispatchQueue.global()` (like
/// WatchConnectivity's own queue) with the reply the test programmed.
final class FakeWatchSession: WatchSessionProtocol, @unchecked Sendable {
    enum Answer {
        case reply([String: Any])
        case error(any Error)
        /// Never answers (the timeout ends the wait).
        case silence
    }

    var isPaired = true
    var isWatchAppInstalled = true
    var isReachable = true
    var receivedApplicationContext: [String: Any] = [:]
    var answer: Answer = .reply(["ok": true])

    private let lock = NSLock()
    private var _sent: [[String: Any]] = []
    private var _transfers: [[String: Any]] = []
    private var _outstanding: [[String: Any]] = []
    private var _cancelled: [[String: Any]] = []
    var sent: [[String: Any]] { lock.withLock { _sent } }
    /// Every `transferUserInfo`, delivered or not.
    var transfers: [[String: Any]] { lock.withLock { _transfers } }
    /// Transfers not delivered yet (WCSession's `outstandingUserInfoTransfers`).
    var outstanding: [[String: Any]] {
        get { lock.withLock { _outstanding } }
        set { lock.withLock { _outstanding = newValue } }
    }
    var cancelled: [[String: Any]] { lock.withLock { _cancelled } }

    func sendMessage(
        _ message: [String: Any], replyHandler: (@Sendable ([String: Any]) -> Void)?,
        errorHandler: (@Sendable (any Error) -> Void)?
    ) {
        lock.withLock { _sent.append(message) }
        switch answer {
        case .reply(let reply):
            nonisolated(unsafe) let reply = reply
            DispatchQueue.global().async { replyHandler?(reply) }
        case .error(let error):
            DispatchQueue.global().async { errorHandler?(error) }
        case .silence:
            break
        }
    }

    func transferUserInfo(_ userInfo: [String: Any]) {
        lock.withLock {
            _transfers.append(userInfo)
            _outstanding.append(userInfo)
        }
    }

    func cancelOutstandingTransfers(where shouldCancel: ([String: Any]) -> Bool) {
        lock.withLock {
            _cancelled += _outstanding.filter(shouldCancel)
            _outstanding.removeAll(where: shouldCancel)
        }
    }
}

/// A clock tests move by hand (Unix seconds).
final class TestClock: Sendable {
    let value = Mutex<Double>(1_000)
}

@MainActor
struct WatchLinkTests {
    let server = ManagedServer(id: "srv-1", name: "Home", url: URL(string: "https://home.example.com:8443")!, token: "wc_pat_secret")

    private func grant(code: String = "12345678", expires: Double = 1_600) -> PairingCodeGrant {
        // The code's own address differs from the one this iPhone reaches the server at (M9).
        PairingCodeGrant(code: code, expiresAt: expires, serverUrl: "https://public.example.com", viaDirectory: false)
    }

    /// The link's clock: 1 000 s (Unix) unless a test moves it.
    let clock = TestClock()

    private func makeLink(
        _ session: FakeWatchSession?, timeout: Duration = .seconds(15),
        sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) -> WatchLink {
        let clock = clock
        return WatchLink(session: session, timeout: timeout, now: { Date(timeIntervalSince1970: clock.value.withLock { $0 }) }, sleep: sleep)
    }

    private func pairTransfer(code: String, expires: Double?) -> [String: Any] {
        var dictionary: [String: Any] = ["v": 1, "type": "pair", "server_url": "https://home.example.com", "code": code, "name": "Home"]
        if let expires { dictionary["expires_at"] = expires }
        return dictionary
    }

    private func api() -> FakeServerAPI {
        let api = FakeServerAPI()
        api.grant = grant()
        return api
    }

    @Test func sendWhenReachableWaitsReply() async {
        let session = FakeWatchSession()
        let link = makeLink(session)
        let api = api()

        let result = await link.send(server: server, api: api)

        #expect(result == .paired)
        #expect(api.calls == ["createPairingCode"])
        #expect(session.sent.count == 1)
        #expect(session.transfers.isEmpty)
        let sent = WatchLinkMessage(session.sent[0])
        #expect(sent == .pair(server: server.url, code: PairingCode("12345678")!, name: "Home", expiresAt: 1_600))
    }

    /// M9: the watch gets the address this iPhone reaches the server at, not the code's `server_url`.
    @Test func sendsManagedURL() async {
        let session = FakeWatchSession()
        let link = makeLink(session)
        _ = await link.send(server: server, api: api())
        #expect(session.sent.first?["server_url"] as? String == "https://home.example.com:8443")
    }

    /// Tokens never cross WatchConnectivity: only address, code and name.
    @Test func sendCarriesNoToken() async {
        let session = FakeWatchSession()
        let link = makeLink(session)
        _ = await link.send(server: server, api: api())
        let sent = session.sent.first ?? [:]
        #expect(Set(sent.keys) == ["v", "type", "server_url", "code", "name", "expires_at"])
        #expect(!sent.values.contains { ($0 as? String)?.contains("wc_pat_") == true })
    }

    @Test func sendWhenNotReachableQueues() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let link = makeLink(session)

        let result = await link.send(server: server, api: api())

        #expect(result == .queued)
        #expect(session.sent.isEmpty)
        #expect(session.transfers.count == 1)
        #expect(session.transfers.first?["code"] as? String == "12345678")
        #expect(WatchLinkMessage(session.transfers[0]) == .pair(server: server.url, code: PairingCode("12345678")!, name: "Home", expiresAt: 1_600))
    }

    @Test func sendFailureShowsError() async {
        let session = FakeWatchSession()
        session.answer = .reply(["ok": false, "error": "busy"])
        let link = makeLink(session)
        #expect(await link.send(server: server, api: api()) == .failed("busy"))
    }

    @Test func sendErrorShowsError() async {
        let session = FakeWatchSession()
        session.answer = .error(URLError(.timedOut))
        let link = makeLink(session)
        #expect(await link.send(server: server, api: api()) == .failed(WatchLink.Message.unreachable))
    }

    /// I3: no answer in time is not a failure: the watch may still be pairing (slow network, or
    /// waiting for an approval it could not report).
    @Test func sendTimeoutIsStillPairing() async {
        let session = FakeWatchSession()
        session.answer = .silence
        let link = makeLink(session, timeout: .milliseconds(50))
        #expect(await link.send(server: server, api: api()) == .stillPairing)
    }

    /// I3: a server in manual mode: the watch says so at once; the owner approves on this iPhone.
    @Test func pendingReplyAsksForApproval() async {
        let session = FakeWatchSession()
        session.answer = .reply(["ok": true, "pending": true, "request_id": "4821"])
        let link = makeLink(session)
        #expect(await link.send(server: server, api: api()) == .pending(requestId: "4821"))
    }

    /// Minor 1: the watch's short reasons become words.
    @Test func reasonsBecomeText() {
        #expect(WatchLink.text(forReason: "busy") == WatchLink.Message.busy)
        #expect(WatchLink.text(forReason: "invalid") == WatchLink.Message.unexpected)
        #expect(WatchLink.text(forReason: "unsupported") == WatchLink.Message.unexpected)
        #expect(WatchLink.text(forReason: "expired") == WatchLink.Message.expired)
        #expect(WatchLink.text(forReason: "Invalid or expired code.") == "Invalid or expired code.")
        #expect(WatchLink.SendResult.failed("busy").summary == WatchLink.Message.busy)
    }

    @Test func malformedReplyFails() async {
        let session = FakeWatchSession()
        session.answer = .reply(["what": 1])
        let link = makeLink(session)
        #expect(await link.send(server: server, api: api()) == .failed(WatchLink.Message.unexpected))
    }

    @Test func unavailableWithoutPairedWatch() async {
        let session = FakeWatchSession()
        session.isPaired = false
        let link = makeLink(session)
        let api = api()

        #expect(!link.canReachWatch)
        #expect(await link.send(server: server, api: api) == .unavailable)
        #expect(api.calls.isEmpty)
        #expect(session.sent.isEmpty && session.transfers.isEmpty)
    }

    @Test func unavailableWithoutWatchApp() async {
        let session = FakeWatchSession()
        session.isWatchAppInstalled = false
        let link = makeLink(session)
        let api = api()
        #expect(await link.send(server: server, api: api) == .unavailable)
        #expect(api.calls.isEmpty)
    }

    @Test func unavailableWithoutWatchConnectivity() async {
        let link = makeLink(nil)
        let api = api()
        #expect(!link.canReachWatch)
        #expect(await link.send(server: server, api: api) == .unavailable)
        #expect(api.calls.isEmpty)
        #expect(!link.isOnWatch(server))
    }

    @Test func codeFailureShowsError() async {
        let session = FakeWatchSession()
        let link = makeLink(session)
        let api = FakeServerAPI()
        api.pairingCodeError = APIError.unavailable("Device limit reached.")
        #expect(await link.send(server: server, api: api) == .failed("Device limit reached."))
        #expect(session.sent.isEmpty)
    }

    /// M8: a new code makes queued `pair` transfers with an older code useless; they are cancelled.
    @Test func newCodeCancelsQueuedPair() async {
        let session = FakeWatchSession()
        session.isReachable = false
        session.outstanding = [["v": 1, "type": "refresh"]]
        let link = makeLink(session)
        _ = await link.send(server: server, api: api())
        _ = await link.send(server: server, grant: grant(code: "87654321"))
        #expect(session.cancelled.map { $0["code"] as? String } == ["12345678"])
        #expect(session.outstanding.map { $0["code"] as? String } == [nil, "87654321"])
        #expect(session.transfers.map { $0["code"] as? String } == ["12345678", "87654321"])
    }

    /// M8: once the code expires (while the app runs), its queued transfer is cancelled.
    @Test func expiredCodeCancelsQueuedPair() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let slept = Mutex<[Duration]>([])
        let clock = clock
        let link = makeLink(session, sleep: { duration in
            slept.withLock { $0.append(duration) }
            clock.value.withLock { $0 += Double(duration.components.seconds) }
        })
        _ = await link.send(server: server, grant: grant(expires: 1_600))
        await waitUntil { session.outstanding.isEmpty }
        #expect(session.cancelled.map { $0["code"] as? String } == ["12345678"])
        #expect(slept.withLock { $0 } == [.seconds(600)])
    }

    /// I2: the app may be gone when a code expires: at activation and back in the foreground, queued
    /// `pair` transfers whose code expired (or that carry no expiry) are cancelled; the rest stay.
    @Test func sweepCancelsExpiredTransfers() {
        let session = FakeWatchSession()
        session.outstanding = [
            pairTransfer(code: "11111111", expires: 900),
            pairTransfer(code: "22222222", expires: 1_600),
            pairTransfer(code: "33333333", expires: nil),
            ["v": 1, "type": "refresh"],
        ]
        let link = makeLink(session)
        link.sweepExpiredTransfers()
        #expect(session.cancelled.map { $0["code"] as? String } == ["11111111", "33333333"])
        #expect(session.outstanding.map { $0["code"] as? String } == ["22222222", nil])
    }

    /// I2: queued transfers carry the code's expiry, so the watch can ignore a late one.
    @Test func sendsExpiry() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let link = makeLink(session)
        _ = await link.send(server: server, grant: grant(expires: 1_600))
        #expect(session.transfers.first?["expires_at"] as? Double == 1_600)
    }

    /// "Show the code instead" after a queued send: the queued code goes away.
    @Test func cancelQueuedPairDropsTransfers() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let link = makeLink(session)
        _ = await link.send(server: server, api: api())
        link.cancelQueuedPair()
        #expect(session.outstanding.isEmpty)
    }

    @Test func isOnWatchUsesContext() async {
        let session = FakeWatchSession()
        session.receivedApplicationContext = WatchLinkContext(servers: ["https://home.example.com:8443"]).dictionary
        let link = makeLink(session)
        #expect(link.watchServers == ["https://home.example.com:8443"])
        #expect(link.isOnWatch(server))
        // The canonical form: case and default port do not matter.
        let other = ManagedServer(name: "Same", url: URL(string: "https://HOME.example.com:8443/")!, token: "t")
        #expect(link.isOnWatch(other))
        let elsewhere = ManagedServer(name: "Work", url: URL(string: "https://work.example.com")!, token: "t")
        #expect(!link.isOnWatch(elsewhere))
    }

    /// Minor 4: a watch unpaired (or without the app any more) is no longer "On watch".
    @Test func isOnWatchNeedsTheWatch() {
        let session = FakeWatchSession()
        session.receivedApplicationContext = WatchLinkContext(servers: ["https://home.example.com:8443"]).dictionary
        let link = makeLink(session)
        session.isWatchAppInstalled = false
        link.sessionStateDidChange()
        #expect(!link.isOnWatch(server))
    }

    @Test func contextUpdates() {
        let session = FakeWatchSession()
        let link = makeLink(session)
        #expect(!link.isOnWatch(server))
        link.contextDidChange(WatchLinkContext(servers: ["https://home.example.com:8443"]))
        #expect(link.isOnWatch(server))
    }

    @Test func refreshSendsMessage() async {
        let session = FakeWatchSession()
        let link = makeLink(session)
        #expect(await link.refreshWatch())
        #expect(session.sent.map { WatchLinkMessage($0) } == [.refresh])
    }

    @Test func refreshNeedsReachableWatch() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let link = makeLink(session)
        #expect(!(await link.refreshWatch()))
        #expect(session.sent.isEmpty && session.transfers.isEmpty)
    }

    /// M7: the watch's login code is shown until it expires; an expired one is dropped.
    @Test func deviceCodeShownUntilExpiry() {
        let link = makeLink(FakeWatchSession())
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 999))
        #expect(link.incomingDeviceCode == nil)
        link.receive(.deviceCode(userCode: "zxsg-kcpn", expiresAt: 1_600))
        #expect(link.incomingDeviceCode == "ZXSG-KCPN")
        clock.value.withLock { $0 = 1_600 }
        #expect(link.incomingDeviceCode == nil)
    }

    private func waitUntil(_ condition: () -> Bool) async {
        for _ in 0..<200 where !condition() {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }
}
