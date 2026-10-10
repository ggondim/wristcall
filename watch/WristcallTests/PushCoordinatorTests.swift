import Foundation
import Synchronization
import Testing
import UserNotifications
import WristcallKit
@testable import Wristcall

/// Plays the push relay (the Cloud). Keys are `wc_push_<n>`; the test can make the relay forget one.
final class FakePushRelay: PushRelaying {
    private struct State {
        var calls: [String] = []
        var live: Set<String> = []
        var next = 0
        var registerErrors: [String: PairingError] = [:]
    }

    private let state = Mutex(State())

    /// One line per request, e.g. `"register tag=srv-a label=a.example.com token=0a0b"`.
    var calls: [String] { state.withLock { $0.calls } }
    var liveKeys: Set<String> { state.withLock { $0.live } }

    /// The relay lost the key (a `410` on `GET /v1/push/registrations/current`).
    func forget(_ key: String) {
        state.withLock { _ = $0.live.remove(key) }
    }

    func failRegister(tag: String, with error: PairingError) {
        state.withLock { $0.registerErrors[tag] = error }
    }

    func register(
        deviceToken: Data, topic: String, environment: PushEnvironment, label: String, tag: String
    ) async throws -> String {
        try state.withLock { state in
            let hex = deviceToken.map { String(format: "%02x", $0) }.joined()
            state.calls.append("register tag=\(tag) label=\(label) token=\(hex) topic=\(topic) env=\(environment.rawValue)")
            if let error = state.registerErrors[tag] { throw error }
            state.next += 1
            let key = "wc_push_\(state.next)"
            state.live.insert(key)
            return key
        }
    }

    func isRegistered(pushKey: String) async throws -> Bool {
        state.withLock { state in
            state.calls.append("isRegistered \(pushKey)")
            return state.live.contains(pushKey)
        }
    }

    func unregister(pushKey: String) async throws {
        state.withLock { state in
            state.calls.append("unregister \(pushKey)")
            state.live.remove(pushKey)
        }
    }
}

/// Plays the paired servers' push routes, by host.
final class FakeServerPush: Sendable {
    private struct State {
        var calls: [String] = []
        var health: [String: Result<ServerHealth, PairingError>] = [:]
        var setErrors: [String: PairingError] = [:]
    }

    private let state = Mutex(State())

    /// One line per request, e.g. `"put a.example.com wc_push_1"`.
    var calls: [String] { state.withLock { $0.calls } }

    func setHealth(_ result: Result<ServerHealth, PairingError>, for host: String) {
        state.withLock { $0.health[host] = result }
    }

    func failSetPushKey(host: String, with error: PairingError) {
        state.withLock { $0.setErrors[host] = error }
    }

    func client(for credentials: Credentials) -> any ServerPushing {
        Client(host: credentials.serverURL.host() ?? "", owner: self)
    }

    fileprivate func health(_ host: String) throws -> ServerHealth {
        try state.withLock { state in
            state.calls.append("health \(host)")
            return state.health[host] ?? .failure(.unexpectedStatus(599))
        }.get()
    }

    fileprivate func put(_ host: String, _ key: String) throws {
        try state.withLock { state in
            state.calls.append("put \(host) \(key)")
            if let error = state.setErrors[host] { throw error }
        }
    }

    fileprivate func clear(_ host: String) {
        state.withLock { $0.calls.append("clear \(host)") }
    }

    private struct Client: ServerPushing {
        let host: String
        let owner: FakeServerPush

        func health() async throws -> ServerHealth { try owner.health(host) }
        func setPushKey(_ key: String) async throws { try owner.put(host, key) }
        func clearPushKey() async throws { owner.clear(host) }
    }
}

final class InMemoryPushKeyStore: PushKeyStore {
    private let keys = Mutex<[String: StoredPushKey]>([:])

    var all: [String: StoredPushKey] { keys.withLock { $0 } }

    func load(serverID: String) throws -> StoredPushKey? { keys.withLock { $0[serverID] } }
    func save(_ key: StoredPushKey, serverID: String) throws { keys.withLock { $0[serverID] = key } }
    func delete(serverID: String) throws { keys.withLock { _ = $0.removeValue(forKey: serverID) } }
}

/// Records what `AppModel` tells its push handler, and how many `DELETE /v1/me` had gone out by then.
@MainActor
final class RecordingPushHandler: PushHandling {
    let pairing: StubPairingService
    private(set) var events: [String] = []

    init(pairing: StubPairingService) {
        self.pairing = pairing
    }

    func serverAdded(_ credentials: Credentials) {
        events.append("added \(credentials.id)")
    }

    func serverWillBeRemoved(_ credentials: Credentials) async {
        events.append("willRemove \(credentials.id) unpairs=\(pairing.unpairTokens.count)")
    }

    func serverRemoved(_ credentials: Credentials) {
        events.append("removed \(credentials.id) unpairs=\(pairing.unpairTokens.count)")
    }

    func oneWayResultShown() {
        events.append("oneWayResultShown")
    }
}

/// Task 12: one push key per paired server, resynchronized at every activation, and results
/// brought by notifications.
@MainActor
struct PushCoordinatorTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let handler = StubCallHandler()
    let clock = FakeClock()
    let relay = FakePushRelay()
    let servers = FakeServerPush()
    let keys = InMemoryPushKeyStore()
    let defaults = UserDefaults(suiteName: "PushCoordinatorTests.\(UUID().uuidString)")!
    let relayURL = URL(string: "http://127.0.0.1:8090")!
    let serverA = URL(string: "https://a.example.com")!
    let serverB = URL(string: "https://b.example.com")!
    let token = Data([0x0a, 0x0b, 0x0c])
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [
        Agent(id: "ag_1", slug: "default", displayName: "Agent"),
        Agent(id: "ag_2", slug: "notes", displayName: "Notes", icon: "note.text", callType: .oneShot),
    ])

    /// Two servers at Home, both announcing the app's relay.
    func makeModel() async throws -> AppModel {
        try store.save([
            Credentials(serverURL: serverA, deviceId: "dev-a", token: "ta", id: "srv-a"),
            Credentials(serverURL: serverB, deviceId: "dev-b", token: "tb", id: "srv-b"),
        ])
        pairing.setMeResults([.success(info)], for: serverA)
        pairing.setMeResults([.success(info)], for: serverB)
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: relayURL)), for: "a.example.com")
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: relayURL)), for: "b.example.com")
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller,
            pendingResults: PendingResultStore(defaults: defaults))
        model.callHandler = handler
        await model.launch()
        try #require(model.phase == .home)
        return model
    }

    func makeCoordinator(_ model: AppModel, authorizations: Recorder<Int>? = nil) -> PushCoordinator {
        let servers = servers
        let coordinator = PushCoordinator(
            relayURL: relayURL, topic: "io.github.ggondim.wristcall", environment: .sandbox, keys: keys,
            makeRelay: { [relay] _ in relay },
            makeServer: { servers.client(for: $0) },
            requestAuthorization: { authorizations?.append(1) })
        coordinator.model = model
        model.pushHandler = coordinator
        return coordinator
    }

    /// Home, then a one-way call to "Notes" on server A ended normally: its result screen is up.
    func openResult(_ model: AppModel, callID: String = "c_1") throws -> CallResultModel {
        let notes = try #require(model.agents.first { $0.serverID == "srv-a" && $0.agent.id == "ag_2" })
        model.startCall(notes)
        model.callDidEnd(.normal, callID: callID)
        try #require(model.phase == .callResult)
        return try #require(model.callResult)
    }

    func finished(_ tag: String, _ callID: String) -> PushMessage {
        .callFinished(tag: tag, callID: callID, state: .delivered, failure: nil, agentID: "ag_2")
    }

    // MARK: - Registration

    @Test func registersOneKeyPerServerLabelledWithItsHost() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        #expect(relay.calls == [
            "register tag=srv-a label=a.example.com token=0a0b0c topic=io.github.ggondim.wristcall env=sandbox",
            "register tag=srv-b label=b.example.com token=0a0b0c topic=io.github.ggondim.wristcall env=sandbox",
        ])
        #expect(servers.calls.filter { $0.hasPrefix("put") } == ["put a.example.com wc_push_1", "put b.example.com wc_push_2"])
        #expect(keys.all["srv-a"] == StoredPushKey(pushKey: "wc_push_1", deviceToken: "0a0b0c"))
        #expect(keys.all["srv-b"] == StoredPushKey(pushKey: "wc_push_2", deviceToken: "0a0b0c"))
    }

    @Test func sameTokenAndLiveKeyOnlyPutsTheKeyAgain() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value

        await coordinator.sceneDidBecomeActive()?.value

        #expect(relay.calls.filter { $0.hasPrefix("register") }.count == 2)
        #expect(relay.calls.suffix(2) == ["isRegistered wc_push_1", "isRegistered wc_push_2"])
        #expect(servers.calls.filter { $0.hasPrefix("put") }.suffix(2) == [
            "put a.example.com wc_push_1", "put b.example.com wc_push_2",
        ])
    }

    @Test func aKeyTheRelayForgotIsRegisteredAgain() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value
        relay.forget("wc_push_1")

        await coordinator.sceneDidBecomeActive()?.value

        #expect(relay.calls.filter { $0.hasPrefix("register tag=srv-a") }.count == 2)
        #expect(relay.calls.filter { $0.hasPrefix("register tag=srv-b") }.count == 1)
        #expect(keys.all["srv-a"]?.pushKey == "wc_push_3")
        #expect(servers.calls.last { $0.hasPrefix("put a.example.com") } == "put a.example.com wc_push_3")
        #expect(keys.all["srv-b"]?.pushKey == "wc_push_2")
    }

    @Test func aServerAnnouncingAnotherRelayIsSkipped() async throws {
        let model = try await makeModel()
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: URL(string: "https://evil.example")!)), for: "b.example.com")
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        #expect(!relay.calls.contains { $0.contains("tag=srv-b") })
        #expect(!servers.calls.contains { $0.hasPrefix("put b.example.com") })
        #expect(keys.all["srv-b"] == nil)
        #expect(keys.all["srv-a"] != nil)
    }

    @Test func theSameRelayWrittenAnotherWayIsAccepted() async throws {
        let model = try await makeModel()
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: URL(string: "HTTP://127.0.0.1:8090/")!)), for: "b.example.com")
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        #expect(keys.all["srv-b"] != nil)
        #expect(servers.calls.contains { $0.hasPrefix("put b.example.com") })
    }

    @Test(arguments: [
        ("https://cloud.example.com", "https://cloud.example.com/"),
        ("https://cloud.example.com", "HTTPS://Cloud.Example.com:443"),
        ("https://cloud.example.com/relay", "https://cloud.example.com:443/relay/"),
        ("http://localhost", "http://LOCALHOST:80/"),
        ("http://127.0.0.1:8090", "http://127.0.0.1:8090/"),
    ])
    func sameRelayIgnoresCaseDefaultPortAndTrailingSlash(_ lhs: String, _ rhs: String) {
        #expect(PushCoordinator.sameRelay(URL(string: lhs)!, URL(string: rhs)!))
    }

    @Test(arguments: [
        ("https://cloud.example.com", "http://cloud.example.com"),
        ("https://cloud.example.com", "https://cloud.example.com:8443"),
        ("http://localhost", "http://localhost:443"),
        ("https://cloud.example.com", "https://evil.example.com"),
        ("https://cloud.example.com/relay", "https://cloud.example.com/other"),
        ("https://cloud.example.com/Relay", "https://cloud.example.com/relay"),
        ("https://cloud.example.com", "https://user@cloud.example.com"),
        ("https://cloud.example.com?a=1", "https://cloud.example.com?a=1"),
    ])
    func sameRelayTellsDifferentRelaysApart(_ lhs: String, _ rhs: String) {
        #expect(!PushCoordinator.sameRelay(URL(string: lhs)!, URL(string: rhs)!))
    }

    @Test func aLongHostIsCutTo64CharactersInTheLabel() async throws {
        // The relay refuses a label over 64 characters: the registration would fail for good.
        let host = String(repeating: "a", count: 60) + ".example.com"
        let url = URL(string: "https://\(host)")!
        try store.save([Credentials(serverURL: url, deviceId: "dev-l", token: "tl", id: "srv-l")])
        pairing.setMeResults([.success(info)], for: url)
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: relayURL)), for: host)
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller,
            pendingResults: PendingResultStore(defaults: defaults))
        await model.launch()
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        let label = String(host.prefix(64))
        #expect(relay.calls == [
            "register tag=srv-l label=\(label) token=0a0b0c topic=io.github.ggondim.wristcall env=sandbox",
        ])
        #expect(keys.all["srv-l"]?.pushKey == "wc_push_1")
    }

    @Test func registrationLabelIsTheHostUpTo64Characters() {
        #expect(PushCoordinator.registrationLabel(for: URL(string: "https://a.example.com")!) == "a.example.com")
        let long = String(repeating: "b", count: 80) + ".example.com"
        #expect(PushCoordinator.registrationLabel(for: URL(string: "https://\(long)")!) == String(repeating: "b", count: 64))
        #expect(PushCoordinator.registrationLabel(for: URL(string: "file:///tmp/x")!) == "wristcall")
    }

    @Test func aServerWithoutPushIsSkipped() async throws {
        let model = try await makeModel()
        // 0.5.0 (no `push` in health) and a relay that has push off (`404` on register).
        servers.setHealth(.success(ServerHealth(version: "0.5.0", relay: nil)), for: "a.example.com")
        relay.failRegister(tag: "srv-b", with: .notFound)
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        #expect(!relay.calls.contains { $0.contains("tag=srv-a") })
        #expect(!servers.calls.contains { $0.hasPrefix("put") })
        #expect(keys.all.isEmpty)
        #expect(model.message == nil)
    }

    @Test func anErrorOnOneServerDoesNotStopTheOthers() async throws {
        let model = try await makeModel()
        servers.setHealth(.failure(.network(.timedOut)), for: "a.example.com")
        let coordinator = makeCoordinator(model)

        await coordinator.didRegister(token: token)?.value

        #expect(keys.all["srv-a"] == nil)
        #expect(keys.all["srv-b"]?.pushKey == "wc_push_1")
        #expect(servers.calls.contains("put b.example.com wc_push_1"))
    }

    @Test func aNewTokenUnregistersTheOldKeyFirst() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value

        await coordinator.didRegister(token: Data([0xff]))?.value

        let calls = Array(relay.calls.dropFirst(2))
        #expect(calls == [
            "unregister wc_push_1",
            "register tag=srv-a label=a.example.com token=ff topic=io.github.ggondim.wristcall env=sandbox",
            "unregister wc_push_2",
            "register tag=srv-b label=b.example.com token=ff topic=io.github.ggondim.wristcall env=sandbox",
        ])
        #expect(relay.liveKeys == ["wc_push_3", "wc_push_4"])
        #expect(keys.all["srv-a"] == StoredPushKey(pushKey: "wc_push_3", deviceToken: "ff"))
    }

    @Test func nothingHappensWithoutAToken() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)

        #expect(coordinator.sceneDidBecomeActive() == nil)
        #expect(relay.calls.isEmpty)
        #expect(servers.calls.isEmpty)
    }

    @Test func anAddedServerGetsItsKey() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value
        let serverC = URL(string: "https://c.example.com")!
        servers.setHealth(.success(ServerHealth(version: "0.6.0", relay: relayURL)), for: "c.example.com")
        model.addServer()
        model.useServerURL(serverC.absoluteString)
        pairing.pairResults = [.success(.paired(PairedDevice(deviceId: "dev-c", token: "tc")))]
        pairing.setMeResults([.success(info)], for: serverC)

        await model.pair(code: try #require(PairingCode("12345678"))).value
        let id = try #require(model.servers.first { $0.credentials.serverURL == serverC }?.id)
        await waitUntil { keys.all[id] != nil }

        #expect(relay.calls.contains { $0.hasPrefix("register tag=\(id) label=c.example.com") })
        #expect(servers.calls.contains { $0.hasPrefix("put c.example.com") })
    }

    @Test func theRelayComesFromTheBundleOnly() {
        #expect(PushCoordinator.relayURL(fromInfoValue: nil) == nil)
        #expect(PushCoordinator.relayURL(fromInfoValue: "") == nil)
        #expect(PushCoordinator.relayURL(fromInfoValue: "http://127.0.0.1:8090") == relayURL)
    }

    @Test func notificationsAreAskedForOnceAtTheFirstOneWayResult() async throws {
        let model = try await makeModel()
        let authorizations = Recorder<Int>()
        _ = makeCoordinator(model, authorizations: authorizations)
        _ = pairing.holdCallStatus()
        #expect(authorizations.all.isEmpty)

        _ = try openResult(model)
        model.dismissResult()
        _ = try openResult(model, callID: "c_2")
        await waitUntil { !authorizations.all.isEmpty }

        #expect(authorizations.all.count == 1)
    }

    // MARK: - Removing a server

    @Test func removingAServerClearsTheKeyBeforeUnpairingThenForgetsIt() async throws {
        let model = try await makeModel()
        let recorder = RecordingPushHandler(pairing: pairing)
        model.pushHandler = recorder

        await model.removeServer(id: "srv-a")

        #expect(recorder.events == ["willRemove srv-a unpairs=0", "removed srv-a unpairs=1"])
    }

    @Test func removalClearsTheServerThenUnregistersAndDeletesTheKey() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value
        let credentials = try #require(model.servers.first { $0.id == "srv-a" }?.credentials)

        await coordinator.serverWillBeRemoved(credentials)
        await coordinator.forgetKey(of: credentials)

        #expect(servers.calls.last == "clear a.example.com")
        #expect(relay.calls.last == "unregister wc_push_1")
        #expect(keys.all["srv-a"] == nil)
        #expect(keys.all["srv-b"] != nil)
    }

    @Test func removingAServerThroughTheModelForgetsItsKey() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        await coordinator.didRegister(token: token)?.value

        await model.removeServer(id: "srv-a")
        await waitUntil { keys.all["srv-a"] == nil }

        #expect(servers.calls.contains("clear a.example.com"))
        #expect(relay.calls.contains("unregister wc_push_1"))
        #expect(keys.all["srv-a"] == nil)
        #expect(pairing.unpairTokens == ["ta"])
    }

    // MARK: - Notifications

    @Test func aPushForTheOpenResultEndsTheWaitWithoutABanner() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        let gate = pairing.holdCallStatus()
        let result = try openResult(model)
        await waitUntil { pairing.callStatusCount == 1 }

        let options = coordinator.willPresent(finished("srv-a", "c_1"))

        #expect(options.isEmpty)
        // `pushArrived()` dropped the waiting query and asked afresh.
        await waitUntil { pairing.callStatusCount == 2 }
        #expect(pairing.callStatusCount == 2)
        #expect(model.callResult === result)
        gate.open()
    }

    @Test func aPushForAnotherCallShowsABannerAndLeavesTheResultAlone() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        let gate = pairing.holdCallStatus()
        let result = try openResult(model)
        await waitUntil { pairing.callStatusCount == 1 }

        let otherCall = coordinator.willPresent(finished("srv-a", "c_2"))
        let otherServer = coordinator.willPresent(finished("srv-b", "c_1"))
        let notACall = coordinator.willPresent(.other(event: "test"))

        #expect(otherCall.contains(.banner))
        #expect(otherServer.contains(.banner))
        #expect(notACall.contains(.banner))
        try await Task.sleep(for: .milliseconds(50))
        #expect(pairing.callStatusCount == 1)
        #expect(model.callResult === result)
        #expect(result.isChecking)
        gate.open()
    }

    @Test func tappingAPushOpensThatCallsResult() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        pairing.callStatusResults = [.success(CallStatus(
            id: "c_7", callType: .oneShot, state: .delivered, failure: nil, text: "buy milk", attempts: nil))]

        coordinator.didReceive(finished("srv-b", "c_7"))

        #expect(model.phase == .callResult)
        let result = try #require(model.callResult)
        #expect(result.callID == "c_7")
        #expect(result.target.serverID == "srv-b")
        #expect(result.target.agent.displayName == "Notes")
        await waitUntil { !result.isChecking }
        #expect(result.state == .finished(CallStatus(
            id: "c_7", callType: .oneShot, state: .delivered, failure: nil, text: "buy milk", attempts: nil)))
        #expect(pairing.calls.last == "callStatus https://b.example.com c_7")
    }

    @Test func tappingAPushOfTheOpenResultAsksAgain() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)
        let gate = pairing.holdCallStatus()
        let result = try openResult(model)
        await waitUntil { pairing.callStatusCount == 1 }

        coordinator.didReceive(finished("srv-a", "c_1"))

        await waitUntil { pairing.callStatusCount == 2 }
        #expect(model.callResult === result)
        gate.open()
    }

    @Test func tappingAPushOfAnUnknownServerOpensNothing() async throws {
        let model = try await makeModel()
        let coordinator = makeCoordinator(model)

        coordinator.didReceive(finished("srv-gone", "c_7"))

        #expect(model.phase == .home)
        #expect(model.callResult == nil)
    }

    @Test func aPushTappedBeforeLaunchOpensOnceTheServerAnswers() async throws {
        try store.save([Credentials(serverURL: serverA, deviceId: "dev-a", token: "ta", id: "srv-a")])
        pairing.setMeResults([.success(info)], for: serverA)
        pairing.callStatusResults = [.success(CallStatus(
            id: "c_7", callType: .oneShot, state: .delivered, failure: nil, text: nil, attempts: nil))]
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller,
            pendingResults: PendingResultStore(defaults: defaults))
        let coordinator = makeCoordinator(model)

        coordinator.didReceive(finished("srv-a", "c_7"))
        #expect(model.callResult == nil)
        await model.launch()

        #expect(model.phase == .callResult)
        #expect(model.callResult?.callID == "c_7")
    }
}
