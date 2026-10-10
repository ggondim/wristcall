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
    private var _cancelledTypes: [String] = []
    var sent: [[String: Any]] { lock.withLock { _sent } }
    var transfers: [[String: Any]] { lock.withLock { _transfers } }
    var cancelledTypes: [String] { lock.withLock { _cancelledTypes } }

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
        lock.withLock { _transfers.append(userInfo) }
    }

    func cancelOutstandingTransfers(ofType type: String) {
        lock.withLock { _cancelledTypes.append(type) }
    }
}

@MainActor
struct WatchLinkTests {
    let server = ManagedServer(id: "srv-1", name: "Home", url: URL(string: "https://home.example.com:8443")!, token: "wc_pat_secret")

    private func grant(code: String = "12345678", expires: Double = 1_600) -> PairingCodeGrant {
        // The code's own address differs from the one this iPhone reaches the server at (M9).
        PairingCodeGrant(code: code, expiresAt: expires, serverUrl: "https://public.example.com", viaDirectory: false)
    }

    private func makeLink(
        _ session: FakeWatchSession?, timeout: Duration = .seconds(15),
        sleep: @escaping @Sendable (Duration) async throws -> Void = { try await Task.sleep(for: $0) }
    ) -> WatchLink {
        WatchLink(session: session, timeout: timeout, now: { Date(timeIntervalSince1970: 1_000) }, sleep: sleep)
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
        #expect(sent == .pair(server: server.url, code: PairingCode("12345678")!, name: "Home"))
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
        #expect(Set(sent.keys) == ["v", "type", "server_url", "code", "name"])
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
        #expect(WatchLinkMessage(session.transfers[0]) == .pair(server: server.url, code: PairingCode("12345678")!, name: "Home"))
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

    @Test func sendTimesOut() async {
        let session = FakeWatchSession()
        session.answer = .silence
        let link = makeLink(session, timeout: .milliseconds(50))
        #expect(await link.send(server: server, api: api()) == .failed(WatchLink.Message.noAnswer))
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
        let link = makeLink(session)
        _ = await link.send(server: server, api: api())
        #expect(session.cancelledTypes == ["pair"])
        _ = await link.send(server: server, grant: grant(code: "87654321"))
        #expect(session.cancelledTypes == ["pair", "pair"])
        #expect(session.transfers.map { $0["code"] as? String } == ["12345678", "87654321"])
    }

    /// M8: once the code expires, its queued transfer is cancelled.
    @Test func expiredCodeCancelsQueuedPair() async {
        let session = FakeWatchSession()
        session.isReachable = false
        let slept = Mutex<[Duration]>([])
        let link = makeLink(session, sleep: { duration in slept.withLock { $0.append(duration) } })
        _ = await link.send(server: server, grant: grant(expires: 1_600))
        await waitUntil { session.cancelledTypes.count == 2 }
        #expect(session.cancelledTypes == ["pair", "pair"])
        #expect(slept.withLock { $0 } == [.seconds(600)])
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
        link.receive(.deviceCode(userCode: "ZXSG-KCPN", expiresAt: 1_600))
        #expect(link.incomingDeviceCode == "ZXSG-KCPN")
    }

    private func waitUntil(_ condition: () -> Bool) async {
        for _ in 0..<200 where !condition() {
            try? await Task.sleep(for: .milliseconds(10))
        }
    }
}
