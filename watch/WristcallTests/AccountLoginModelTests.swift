import Foundation
import Synchronization
import Testing
import WristcallKit
@testable import Wristcall

/// Plays the account (Cloud, provider, servers) for `AccountLoginModel`. The HTTP side is the Kit's
/// (`AccountSessionTests`, `AccountPairingTests`): on watchOS, `URLProtocol` stubs never see its requests.
final class FakeAccountBackend: AccountLoginBackend {
    private struct State {
        var signedIn = false
        var start: Result<DeviceAuthorization, any Error> = .failure(OIDCError.malformedResponse)
        var complete: Result<Void, any Error> = .success(())
        var servers: Result<[CloudServer], any Error> = .success([])
        var outcomes: [URL: AccountPairing.Outcome] = [:]
        var calls: [String] = []
    }

    private let state = Mutex(State())

    var calls: [String] { state.withLock { $0.calls } }
    var signedIn: Bool {
        get { state.withLock { $0.signedIn } }
        set { state.withLock { $0.signedIn = newValue } }
    }

    func script(
        start: Result<DeviceAuthorization, any Error>? = nil,
        complete: Result<Void, any Error>? = nil,
        servers: [URL]? = nil,
        outcomes: [URL: AccountPairing.Outcome] = [:]
    ) {
        state.withLock { state in
            if let start { state.start = start }
            if let complete { state.complete = complete }
            if let servers {
                state.servers = .success(servers.enumerated().map { index, url in
                    CloudServer(id: "cs-\(index)", name: "S\(index)", url: url.absoluteString, kind: "self-hosted", linked: true, agents: [])
                })
            }
            state.outcomes.merge(outcomes) { _, new in new }
        }
    }

    func isSignedIn() async -> Bool { signedIn }

    func startDeviceAuthorization() async throws -> DeviceAuthorization {
        try state.withLock { state in
            state.calls.append("start")
            return state.start
        }.get()
    }

    func completeDeviceAuthorization(_ authorization: DeviceAuthorization) async throws {
        try state.withLock { state in
            state.calls.append("complete \(authorization.deviceCode)")
            if case .success = state.complete { state.signedIn = true }
            return state.complete
        }.get()
    }

    func servers() async throws -> [CloudServer] {
        try state.withLock { state in
            state.calls.append("servers")
            return state.servers
        }.get()
    }

    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome {
        state.withLock { state in
            state.calls.append("pair \(server.absoluteString) \(deviceName)")
            return state.outcomes[server] ?? .skipped(server, "unscripted")
        }
    }

    func signOut() async {
        state.withLock { state in
            state.calls.append("signOut")
            state.signedIn = false
        }
    }
}

@MainActor
struct AccountLoginModelTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_800_000_000)

    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let backend = FakeAccountBackend()
    let defaults = UserDefaults(suiteName: "AccountLoginModelTests.\(UUID().uuidString)")!

    var authorization: DeviceAuthorization {
        DeviceAuthorization(
            deviceCode: "device-secret", userCode: "ZXSG-KCPN", verificationURI: URL(string: "https://auth.test/device/")!,
            verificationURIComplete: URL(string: "https://auth.test/device?user_code=ZXSG-KCPN"),
            expiresAt: Self.clock.addingTimeInterval(600), interval: .seconds(5))
    }

    func makeAppModel(sleep: @escaping PairingClient.Sleep = { _ in }) async -> AppModel {
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: sleep)
        await model.launch()
        return model
    }

    func makeLogin(model: AppModel, onSend: (@MainActor (WatchLinkMessage) -> Void)? = nil) -> AccountLoginModel {
        AccountLoginModel(backend: backend, model: model, sendToPhone: { onSend?($0) })
    }

    func info(_ id: String, user: String) -> DeviceInfo {
        DeviceInfo(deviceId: id, deviceName: "Apple Watch", user: UserInfo(id: user, handle: "me"), agents: [])
    }

    // MARK: - Device flow

    @Test func showsCodeAndSendsToPhone() async throws {
        backend.script(start: .success(authorization))
        let model = await makeAppModel()
        let sent = Recorder<WatchLinkMessage>()
        let seen = Recorder<String>()
        let box = LoginBox()
        let login = makeLogin(model: model) { message in
            sent.append(message)
            if let phase = box.login?.phase { seen.append("\(phase)") }
        }
        box.login = login
        await login.signIn()

        let expiresAt = Self.clock.addingTimeInterval(600)
        // The code (and the host and path of the verification URI) shows while the iPhone gets the user code.
        #expect(seen.all.first == "\(AccountLoginModel.Phase.showingCode(userCode: "ZXSG-KCPN", verificationURI: "auth.test/device", expiresAt: expiresAt))")
        #expect(sent.all == [.deviceCode(userCode: "ZXSG-KCPN", expiresAt: expiresAt.timeIntervalSince1970), .signedIn])
        // The device code is the poll's secret: it never goes to the iPhone.
        for message in sent.all {
            #expect(!message.dictionary.values.contains { "\($0)".contains("device-secret") })
        }
        #expect(backend.calls == ["start", "complete device-secret", "servers"])
        #expect(login.isSignedIn)
        #expect(login.isPresented)
        #expect(login.phase == .finished([]))
    }

    @Test func loginExpiredReturnsToStart() async throws {
        backend.script(start: .success(authorization), complete: .failure(OIDCError.expiredToken))
        let model = await makeAppModel()
        let sent = Recorder<WatchLinkMessage>()
        let login = makeLogin(model: model) { sent.append($0) }
        await login.signIn()
        #expect(login.phase == .idle)
        #expect(login.message == AccountLoginModel.Message.expired)
        #expect(login.message == "The code expired. Try again.")
        // Still on the login screen, with "Sign in with account" to try again.
        #expect(login.isPresented)
        #expect(!login.isSignedIn)
        #expect(!backend.calls.contains("servers"))
        #expect(sent.all.count == 1)
    }

    @Test func deniedShowsMessage() async throws {
        backend.script(start: .success(authorization), complete: .failure(OIDCError.accessDenied))
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.signIn()
        #expect(login.phase == .failed("Sign-in was denied."))
        #expect(!login.isSignedIn)
        #expect(!backend.calls.contains("servers"))
    }

    @Test func cloudUnreachableShowsMessage() async throws {
        backend.script(start: .failure(APIError.network(.notConnectedToInternet)))
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.signIn()
        #expect(login.phase == .failed(AccountLoginModel.Message.cloudUnreachable))
    }

    @Test func cancelWhileShowingTheCodeClosesTheScreen() async throws {
        backend.script(start: .success(authorization), complete: .failure(CancellationError()))
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.signIn()
        #expect(!login.isPresented)
        #expect(login.phase == .idle)
        #expect(login.message == nil)
    }

    @Test func unavailableWithoutCloud() async throws {
        let model = await makeAppModel()
        let sent = Recorder<WatchLinkMessage>()
        let login = AccountLoginModel(cloudURL: nil, session: nil, model: model, sendToPhone: { sent.append($0) })
        #expect(!login.isAvailable)
        await login.signIn()
        await login.sync()
        #expect(login.phase == .idle)
        #expect(!login.isPresented)
        #expect(sent.all.isEmpty)
        // A session without the build's Cloud URL counts as no Cloud.
        let session = AccountSession(cloud: URL(string: "https://cloud.test")!, kind: .watch, store: InMemoryTokenStore())
        #expect(!AccountLoginModel(cloudURL: nil, session: session, model: model, sendToPhone: nil).isAvailable)
        #expect(AccountLoginModel(cloudURL: URL(string: "https://cloud.test")!, session: session, model: model, sendToPhone: nil).isAvailable)
    }

    @Test func cloudURLComesOnlyFromASafeBuildValue() {
        #expect(AccountLoginModel.cloudURL(fromInfoValue: nil) == nil)
        #expect(AccountLoginModel.cloudURL(fromInfoValue: "") == nil)
        #expect(AccountLoginModel.cloudURL(fromInfoValue: "$(WRISTCALL_CLOUD_URL)") == nil)
        #expect(AccountLoginModel.cloudURL(fromInfoValue: "http://cloud.test") == nil)
        #expect(AccountLoginModel.cloudURL(fromInfoValue: "https://cloud.test")?.absoluteString == "https://cloud.test")
    }

    // MARK: - Sync

    @Test func syncAddsServers() async throws {
        let direct = URL(string: "https://a.example.com")!
        let approval = URL(string: "https://b.example.com:8443")!
        let request = PairingRequest(requestId: "0423", pollToken: "poll-secret", expiresAt: Self.clock.addingTimeInterval(600))
        backend.signedIn = true
        backend.script(servers: [direct, approval], outcomes: [
            direct: .paired(direct, PairedDevice(deviceId: "dev-a", token: "token-a")),
            approval: .pending(approval, request),
        ])
        pairing.setMeResults([.success(info("dev-a", user: "usr_1"))], for: direct)
        pairing.setMeResults([.success(info("dev-b", user: "usr_2"))], for: approval)
        // The owner approves on the iPhone between the first and the second poll.
        pairing.pollResults = [.success(.pending(requestId: "0423")), .success(.paired(PairedDevice(deviceId: "dev-b", token: "token-b")))]
        // What the login screen shows while the watch waits for the approval (read at each poll's wait).
        let phases = Recorder<String>()
        let box = LoginBox()
        let model = await makeAppModel(sleep: { _ in
            await MainActor.run { if let phase = box.login?.phase { phases.append("\(phase)") } }
        })
        let login = makeLogin(model: model)
        box.login = login
        await login.sync()

        #expect(login.phase == .finished(["a.example.com: added", "b.example.com:8443: added"]))
        #expect(model.servers.map(\.credentials.serverURL) == [direct, approval])
        #expect(model.servers.map(\.credentials.token) == ["token-a", "token-b"])
        #expect(try store.load().count == 2)
        #expect(model.phase == .home)
        #expect(!model.isBusy)
        #expect(pairing.pollTokens == ["poll-secret", "poll-secret"])
        #expect(phases.all == Array(repeating: "\(AccountLoginModel.Phase.awaitingApproval(host: "b.example.com:8443", requestId: "0423"))", count: 2))
        #expect(backend.calls.filter { $0.hasPrefix("pair ") } == [
            "pair https://a.example.com Apple Watch", "pair https://b.example.com:8443 Apple Watch",
        ])
    }

    @Test func syncSkipsPairedAndExplainsSkipped() async throws {
        let paired = URL(string: "https://agent.example.com")!
        let notLinked = URL(string: "https://family.example.com")!
        try store.save([Credentials(serverURL: paired, device: PairedDevice(deviceId: "dev-1", token: "t"))])
        pairing.meResults = [.success(info("dev-1", user: "usr_1"))]
        backend.signedIn = true
        backend.script(
            servers: [URL(string: "https://AGENT.example.com:443/")!, notLinked, URL(string: "http://plain.example.com")!],
            outcomes: [notLinked: .skipped(notLinked, AccountPairing.Reason.notLinked)])
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.sync()
        // The paired server (another spelling) and the plain http:// one are not even tried.
        #expect(backend.calls.filter { $0.hasPrefix("pair ") } == ["pair https://family.example.com Apple Watch"])
        #expect(login.phase == .finished(["family.example.com: not linked to your account"]))
        #expect(model.servers.count == 1)
    }

    @Test func expiredApprovalIsReported() async throws {
        let server = URL(string: "https://b.example.com")!
        backend.signedIn = true
        backend.script(servers: [server], outcomes: [
            server: .pending(server, PairingRequest(requestId: "0423", pollToken: "p", expiresAt: Self.clock)),
        ])
        pairing.pollResults = [.success(.gone)]
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.sync()
        #expect(login.phase == .finished(["b.example.com: \(AppModel.Message.expired)"]))
        #expect(model.servers.isEmpty)
    }

    @Test func agendaSignedOutAsksToSignInAgain() async throws {
        let model = await makeAppModel()
        let login = AccountLoginModel(backend: FailingAgendaBackend(error: AccountError.signedOut), model: model, sendToPhone: nil)
        await login.sync()
        #expect(login.phase == .failed(AccountLoginModel.Message.signInAgain))
        #expect(!login.isSignedIn)
    }

    @Test func cancelWhileAwaitingApprovalStopsTheSync() async throws {
        let server = URL(string: "https://b.example.com")!
        let later = URL(string: "https://c.example.com")!
        backend.signedIn = true
        backend.script(servers: [server, later], outcomes: [
            server: .pending(server, PairingRequest(requestId: "0423", pollToken: "p", expiresAt: Self.clock.addingTimeInterval(600))),
            later: .paired(later, PairedDevice(deviceId: "dev-c", token: "token-c")),
        ])
        pairing.pollResults = Array(repeating: .success(.pending(requestId: "0423")), count: 10_000)
        let model = await makeAppModel(sleep: { _ in try await Task.sleep(for: .milliseconds(5)) })
        let login = makeLogin(model: model)
        let running = Task { await login.sync() }
        await waitUntil { if case .awaitingApproval = login.phase { true } else { false } }
        #expect(model.isBusy)
        login.cancel()
        await running.value
        #expect(login.phase == .finished(["b.example.com: cancelled"]))
        #expect(login.isPresented)
        #expect(!model.isBusy)
        #expect(model.servers.isEmpty)
        #expect(!backend.calls.contains { $0.contains("c.example.com") })
    }

    @Test func busyWatchSkipsTheApprovalWait() async throws {
        // A pairing from the iPhone is waiting for its own approval: the login does not take over.
        let server = URL(string: "https://b.example.com")!
        backend.signedIn = true
        backend.script(servers: [server], outcomes: [
            server: .pending(server, PairingRequest(requestId: "0423", pollToken: "p", expiresAt: Self.clock.addingTimeInterval(600))),
        ])
        pairing.pairResults = [.success(.pending(PairingRequest(requestId: "1111", pollToken: "other", expiresAt: Self.clock)))]
        pairing.pollResults = Array(repeating: .success(.pending(requestId: "1111")), count: 10_000)
        let model = await makeAppModel(sleep: { _ in try await Task.sleep(for: .milliseconds(5)) })
        let other = Task { await model.pair(server: URL(string: "https://x.example.com")!, code: PairingCode("12345678")!) }
        await waitUntil { model.isBusy }
        let login = makeLogin(model: model)
        await login.sync()
        #expect(login.phase == .finished(["b.example.com: \(AccountLoginModel.Message.busy)"]))
        model.cancelPairing()
        _ = await other.value
    }

    @Test func signOutKeepsServers() async throws {
        try store.save([Credentials(serverURL: URL(string: "https://agent.example.com")!, device: PairedDevice(deviceId: "dev-1", token: "t"))])
        pairing.meResults = [.success(info("dev-1", user: "usr_1"))]
        backend.signedIn = true
        let model = await makeAppModel()
        let login = makeLogin(model: model)
        await login.restore()
        #expect(login.isSignedIn)
        await login.signOut()
        #expect(!login.isSignedIn)
        #expect(backend.calls == ["signOut"])
        #expect(model.servers.count == 1)
    }

    @Test func shortForms() {
        #expect(AccountLoginModel.shortForm(URL(string: "https://auth.trigram.com.br/device")!) == "auth.trigram.com.br/device")
        #expect(AccountLoginModel.shortForm(URL(string: "https://auth.test:8443/device/?x=1")!) == "auth.test:8443/device")
        #expect(AccountLoginModel.host(URL(string: "https://SRV.test:443/")!) == "srv.test")
        #expect(AccountLoginModel.host(URL(string: "http://127.0.0.1:8765")!) == "127.0.0.1:8765")
    }
}

/// An account whose every call fails with `error`.
private struct FailingAgendaBackend: AccountLoginBackend {
    let error: AccountError

    func isSignedIn() async -> Bool { false }
    func startDeviceAuthorization() async throws -> DeviceAuthorization { throw error }
    func completeDeviceAuthorization(_ authorization: DeviceAuthorization) async throws { throw error }
    func servers() async throws -> [CloudServer] { throw error }
    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome { .skipped(server, "") }
    func signOut() async {}
}

@MainActor
private final class LoginBox {
    var login: AccountLoginModel?
}
