import Foundation
import Synchronization
import Testing
import WristcallKit
@testable import Wristcall

/// A clock the test moves: `sleep` suspends until `advance(by:)` moves `now` past its deadline (or its task is
/// cancelled), so a 5 s poll or a 10 s timeout passes when the test says so, never on its own.
final class TestClock: Sendable {
    private struct Sleeper {
        let deadline: Date
        let continuation: CheckedContinuation<Void, any Error>
    }

    private struct State {
        var now: Date
        var nextID = 0
        var sleepers: [Int: Sleeper] = [:]
        /// Cancelled before their continuation was registered.
        var cancelled: Set<Int> = []
        var waiters: [Int: (count: Int, continuation: CheckedContinuation<Void, Never>)] = [:]

        /// Waiters whose sleeper count is reached, removed from the list.
        mutating func readyWaiters() -> [CheckedContinuation<Void, Never>] {
            let count = sleepers.count
            let ready = waiters.filter { $0.value.count <= count }
            ready.keys.forEach { waiters.removeValue(forKey: $0) }
            return ready.values.map(\.continuation)
        }
    }

    private let state: Mutex<State>

    init(start: Date = Date(timeIntervalSince1970: 1_800_000_000)) {
        state = Mutex(State(now: start))
    }

    var now: Date { state.withLock { $0.now } }

    /// Tasks sleeping now.
    var sleeperCount: Int { state.withLock { $0.sleepers.count } }

    func sleep(for duration: Duration) async throws {
        let id = state.withLock { state in
            state.nextID += 1
            return state.nextID
        }
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
                let (cancelled, ready) = state.withLock { state -> (Bool, [CheckedContinuation<Void, Never>]) in
                    if state.cancelled.remove(id) != nil { return (true, []) }
                    state.sleepers[id] = Sleeper(
                        deadline: state.now.addingTimeInterval(duration.seconds), continuation: continuation)
                    return (false, state.readyWaiters())
                }
                if cancelled { continuation.resume(throwing: CancellationError()) }
                ready.forEach { $0.resume() }
            }
        } onCancel: {
            let continuation = state.withLock { state in
                guard let sleeper = state.sleepers.removeValue(forKey: id) else {
                    state.cancelled.insert(id)
                    return nil as CheckedContinuation<Void, any Error>?
                }
                return sleeper.continuation
            }
            continuation?.resume(throwing: CancellationError())
        }
    }

    /// Moves `now` and wakes every sleeper whose deadline has passed (they are gone from `sleeperCount` at once).
    func advance(by duration: Duration) {
        let due = state.withLock { state in
            state.now = state.now.addingTimeInterval(duration.seconds)
            let now = state.now
            let due = state.sleepers.filter { $0.value.deadline <= now }.sorted { $0.value.deadline < $1.value.deadline }
            due.forEach { state.sleepers.removeValue(forKey: $0.key) }
            return due.map(\.value.continuation)
        }
        due.forEach { $0.resume() }
    }

    /// Returns once `count` tasks are sleeping (the setup loop went back to waiting for the next poll), or when
    /// the test is cancelled (its time limit), so a broken expectation fails instead of hanging.
    func waitForSleepers(_ count: Int = 1) async {
        let id = state.withLock { state in
            state.nextID += 1
            return state.nextID
        }
        await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                let ready = state.withLock { state in
                    if state.sleepers.count >= count || state.cancelled.remove(id) != nil { return true }
                    state.waiters[id] = (count, continuation)
                    return false
                }
                if ready { continuation.resume() }
            }
        } onCancel: {
            let waiter = state.withLock { state in
                guard let waiter = state.waiters.removeValue(forKey: id) else {
                    state.cancelled.insert(id)
                    return nil as CheckedContinuation<Void, Never>?
                }
                return waiter.continuation
            }
            waiter?.resume()
        }
    }
}

extension Duration {
    var seconds: TimeInterval {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

/// Records, on the main actor, what the setup showed at each backend call and what it announced.
@MainActor
final class SetupProbe {
    weak var setup: SetupModel?
    weak var model: AppModel?
    private(set) var phases: [SetupModel.Phase] = []
    private(set) var showsSetup: [Bool] = []
    /// Backend calls and announcements in order: `"restore"`, `"enroll"`, `"agenda"`, `"pair <host>"`,
    /// `"signOut"`, `"ready acc_1"`, `"ready nil"`.
    private(set) var events: [String] = []
    /// `onAccountReady` values.
    private(set) var announced: [String?] = []
    /// `model.servers.count` at each `onAccountReady`.
    private(set) var serversAtAnnouncement: [Int] = []

    func record(_ event: String) {
        events.append(event)
        if let setup {
            phases.append(setup.phase)
            showsSetup.append(setup.showsSetup)
        }
    }

    func announce(_ account: String?) {
        events.append("ready \(account ?? "nil")")
        announced.append(account)
        serversAtAnnouncement.append(model?.servers.count ?? -1)
        if let setup { showsSetup.append(setup.showsSetup) }
    }
}

/// Scripted Cloud for `SetupModel`. Each call takes the next scripted answer; with none left, `restore` gives
/// `nil`, `enroll` `.nothing`, `agenda` `[]` and `pair` `.skipped(_, needsPhone)`.
final class FakeSetupBackend: SetupBackend {
    struct Enroll: Equatable, Sendable {
        let code: String?
        let tryLegacy: Bool
    }

    private struct State {
        var restore: [String?] = []
        var enroll: [Result<SetupEnrollResult, any Error>] = []
        var agenda: [Result<[CloudServer], any Error>] = []
        var outcomes: [URL: AccountPairing.Outcome] = [:]
        var agendaGate: Gate?
        var signOutGate: Gate?
        var enrolls: [Enroll] = []
        var agendaCount = 0
        var paired: [URL] = []
        var signOutCount = 0
    }

    private let state = Mutex(State())
    let probe: SetupProbe
    /// Open once `agenda()` / `signOut()` has been called.
    let agendaReached = Gate()
    let signOutReached = Gate()

    init(probe: SetupProbe) {
        self.probe = probe
    }

    var enrolls: [Enroll] { state.withLock { $0.enrolls } }
    var agendaCount: Int { state.withLock { $0.agendaCount } }
    var paired: [URL] { state.withLock { $0.paired } }
    var signOutCount: Int { state.withLock { $0.signOutCount } }

    func script(
        restore: [String?]? = nil,
        enroll: [SetupEnrollResult]? = nil,
        agenda: [[URL]]? = nil,
        outcomes: [URL: AccountPairing.Outcome] = [:]
    ) {
        state.withLock { state in
            if let restore { state.restore = restore }
            if let enroll { state.enroll = enroll.map { .success($0) } }
            if let agenda { state.agenda = agenda.map { .success(Self.servers($0)) } }
            state.outcomes.merge(outcomes) { _, new in new }
        }
    }

    func scriptAgenda(_ results: [Result<[URL], any Error>]) {
        state.withLock { state in
            state.agenda = results.map { $0.map(Self.servers) }
        }
    }

    func scriptEnroll(_ results: [Result<SetupEnrollResult, any Error>]) {
        state.withLock { $0.enroll = results }
    }

    /// Holds every `agenda()` answer until the returned gate opens.
    func holdAgenda() -> Gate {
        let gate = Gate()
        state.withLock { $0.agendaGate = gate }
        return gate
    }

    /// Holds `signOut()` until the returned gate opens.
    func holdSignOut() -> Gate {
        let gate = Gate()
        state.withLock { $0.signOutGate = gate }
        return gate
    }

    static func servers(_ urls: [URL]) -> [CloudServer] {
        urls.enumerated().map { index, url in
            CloudServer(id: "cs-\(index)", name: "S\(index)", url: url.absoluteString, kind: "self-hosted",
                        linked: true, agents: [])
        }
    }

    func restore() async -> String? {
        await probe.record("restore")
        return state.withLock { state in
            state.restore.isEmpty ? nil : state.restore.removeFirst()
        }
    }

    func enroll(code: PhoneLinkContext?, tryLegacy: Bool) async throws -> SetupEnrollResult {
        await probe.record("enroll")
        return try state.withLock { state in
            state.enrolls.append(Enroll(code: code?.enrollCode, tryLegacy: tryLegacy))
            return state.enroll.isEmpty ? .success(.nothing) : state.enroll.removeFirst()
        }.get()
    }

    func agenda() async throws -> [CloudServer] {
        await probe.record("agenda")
        let (result, gate) = state.withLock { state in
            state.agendaCount += 1
            return (state.agenda.isEmpty ? .success([]) : state.agenda.removeFirst(), state.agendaGate)
        }
        agendaReached.open()
        await gate?.wait()
        return try result.get()
    }

    func pair(_ server: URL, deviceName: String) async -> AccountPairing.Outcome {
        await probe.record("pair \(server.host() ?? "")")
        return state.withLock { state in
            state.paired.append(server)
            return state.outcomes[server] ?? .skipped(server, AccountPairing.Reason.needsPhone)
        }
    }

    func signOut() async {
        await probe.record("signOut")
        let gate = state.withLock { state in
            state.signOutCount += 1
            return state.signOutGate
        }
        signOutReached.open()
        await gate?.wait()
    }
}

@MainActor
@Suite(.timeLimit(.minutes(1)))
struct SetupModelTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let clock = TestClock()
    let defaults = UserDefaults(suiteName: "SetupModelTests.\(UUID().uuidString)")!
    let probe = SetupProbe()
    let backend: FakeSetupBackend

    let first = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token", id: "srv-1")
    let other = URL(string: "https://home.example.com")!
    let assistant = Agent(id: "ag_1", slug: "default", displayName: "Agent")
    let info = DeviceInfo(
        deviceId: "dev-1", deviceName: "Apple Watch", user: UserInfo(id: "usr_1", handle: "gustavo"),
        agents: [Agent(id: "ag_1", slug: "default", displayName: "Agent")])
    let homeInfo = DeviceInfo(
        deviceId: "dev-9", deviceName: "Apple Watch", user: UserInfo(id: "usr_9", handle: "family"),
        agents: [Agent(id: "ag_9", slug: "house", displayName: "House")])
    let homeDevice = PairedDevice(deviceId: "dev-9", token: "home-token")

    init() {
        backend = FakeSetupBackend(probe: probe)
    }

    /// No server (`.unpaired`), or `first` paired and answered (`.home`).
    func makeModel(paired: Bool = false) async throws -> AppModel {
        if paired {
            try store.save([first])
            pairing.setMeResults([.success(info)], for: first.serverURL)
        }
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in },
                             resultPoller: FakeClock().poller)
        await model.launch()
        try #require(model.phase == (paired ? .home : .unpaired))
        probe.model = model
        return model
    }

    func makeSetup(_ model: AppModel, syncInterval: TimeInterval = 60,
                   isManualRunning: @escaping @MainActor () -> Bool = { false }) -> SetupModel {
        let clock = clock
        let setup = SetupModel(
            backend: backend, model: model, defaults: defaults, timeout: .seconds(10), keyPoll: .seconds(5),
            syncInterval: syncInterval, now: { clock.now }, sleep: { try await clock.sleep(for: $0) })
        setup.isManualRunning = isManualRunning
        let probe = probe
        setup.onAccountReady = { probe.announce($0) }
        probe.setup = setup
        return setup
    }

    func code(_ value: String, account: String? = "acc_1") -> PhoneLinkContext {
        PhoneLinkContext(enrollCode: value, expiresAt: clock.now.timeIntervalSince1970 + 600, account: account)
    }

    // MARK: - First open

    @Test func firstOpenEnrollsByKeyThenGetsAgents() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.enrolled(account: "acc_1")], agenda: [[other]],
                       outcomes: [other: .paired(other, homeDevice)])
        pairing.setMeResults([.success(homeInfo)], for: other)
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()

        #expect(backend.enrolls == [.init(code: nil, tryLegacy: true)])
        #expect(probe.phases == [.idle, .signingIn, .gettingAgents, .gettingAgents])
        #expect(setup.phase == .ready)
        #expect(setup.account == "acc_1")
        #expect(setup.isSignedIn)
        #expect(model.servers.count == 1)
        #expect(model.phase == .home)
        #expect(!setup.showsSetup)
        // A new enrollment is announced only after the first sync (C7).
        #expect(probe.announced == ["acc_1"])
        #expect(probe.serversAtAnnouncement == [1])
        #expect(probe.events == ["restore", "enroll", "agenda", "pair home.example.com", "ready acc_1"])
    }

    @Test func codeArrivingAfterTimeoutEnrolls() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.nothing, .nothing, .enrolled(account: "acc_1")])
        let setup = makeSetup(model)

        await setup.start()
        await clock.waitForSleepers()
        #expect(setup.phase == .signingIn)

        clock.advance(by: .seconds(10))
        await clock.waitForSleepers()
        #expect(setup.phase == .waitingForPhone)
        #expect(setup.showsSetup)

        setup.receive(code("wcc_one"))
        await setup.settle()

        #expect(backend.enrolls == [
            .init(code: nil, tryLegacy: true), .init(code: nil, tryLegacy: false),
            .init(code: "wcc_one", tryLegacy: false),
        ])
        #expect(setup.account == "acc_1")
        #expect(setup.phase == .noAgents)
        #expect(setup.phoneAccount == "acc_1")
        #expect(probe.announced == ["acc_1"])
    }

    @Test func keyArrivingLaterIsPickedUp() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.nothing, .nothing, .nothing, .enrolled(account: "acc_1")])
        let setup = makeSetup(model)

        await setup.start()
        for _ in 0..<3 {
            await clock.waitForSleepers()
            clock.advance(by: .seconds(5))
        }
        await setup.settle()

        #expect(backend.enrolls.count == 4)
        #expect(backend.enrolls.allSatisfy { $0.code == nil })
        #expect(backend.enrolls.map(\.tryLegacy) == [true, false, false, false])
        #expect(setup.account == "acc_1")
        #expect(clock.sleeperCount == 0)
    }

    @Test func refusedCodeIsNotRetried() async throws {
        let model = try await makeModel()
        let setup = makeSetup(model)

        await setup.start()
        await clock.waitForSleepers()

        // Activation and `didReceiveApplicationContext` can hand over the same context twice.
        setup.receive(code("wcc_one"))
        setup.receive(code("wcc_one"))
        await clock.waitForSleepers()
        #expect(backend.enrolls.map(\.code) == [nil, "wcc_one"])

        // Refused: the same code again neither wakes the loop nor is sent again.
        setup.receive(code("wcc_one"))
        #expect(clock.sleeperCount == 1)
        clock.advance(by: .seconds(5))
        await clock.waitForSleepers()

        #expect(backend.enrolls.map(\.code) == [nil, "wcc_one", nil])
        #expect(setup.account == nil)
    }

    @Test func receiveBeforeRestoreDoesNotEnroll() async throws {
        let model = try await makeModel()
        backend.script(restore: ["acc_1"])
        let setup = makeSetup(model)

        setup.receive(code("wcc_one"))
        await setup.start()
        await setup.settle()

        #expect(backend.enrolls.isEmpty)
        #expect(setup.account == "acc_1")
        // A restored session is announced at once, before the agenda (C7).
        #expect(probe.events == ["restore", "ready acc_1", "agenda"])
        #expect(setup.phase == .noAgents)
    }

    @Test func emptyAgendaShowsNoAgents() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.enrolled(account: "acc_1")], agenda: [[]])
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()

        #expect(setup.phase == .noAgents)
        #expect(setup.showsSetup)
        #expect(backend.agendaCount == 1)
        #expect(probe.announced == ["acc_1"])
    }

    @Test func upgradeKeepsHomeAndMigrates() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: [nil], enroll: [.enrolled(account: "acc_1")], agenda: [[first.serverURL]])
        let setup = makeSetup(model)
        #expect(!setup.showsSetup)

        await setup.start()
        await setup.settle()

        #expect(backend.enrolls == [.init(code: nil, tryLegacy: true)])
        #expect(!probe.phases.contains(.signingIn))
        #expect(!probe.phases.contains(.waitingForPhone))
        #expect(!probe.phases.contains(.gettingAgents))
        #expect(!probe.showsSetup.isEmpty)
        #expect(probe.showsSetup.allSatisfy { !$0 })
        #expect(!setup.showsSetup)
        #expect(backend.paired.isEmpty)
        #expect(setup.account == "acc_1")
        #expect(setup.phase == .ready)
        #expect(model.phase == .home)
    }

    @Test func refusedLegacyIsNotRetriedEvery5s() async throws {
        let model = try await makeModel(paired: true)
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()
        #expect(backend.enrolls == [.init(code: nil, tryLegacy: true)])
        #expect(clock.sleeperCount == 0)

        clock.advance(by: .seconds(60))
        await setup.settle()
        #expect(backend.enrolls.count == 1)

        await setup.becameActive()
        await setup.settle()
        #expect(backend.enrolls == [.init(code: nil, tryLegacy: true), .init(code: nil, tryLegacy: true)])
        #expect(!probe.phases.contains(.signingIn))
    }

    @Test func revokedSessionReEnrollsQuietly() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        backend.scriptAgenda([.failure(AccountError.signedOut)])
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()

        #expect(setup.account == nil)
        #expect(!setup.isSignedIn)
        #expect(probe.announced == ["acc_1", nil])
        #expect(backend.enrolls == [.init(code: nil, tryLegacy: true)])
        #expect(!probe.phases.contains(.waitingForPhone))
        #expect(!probe.phases.contains(.signingIn))
        #expect(clock.sleeperCount == 0)
        #expect(model.phase == .home)
    }

    @Test func autoSyncNeverShowsApproval() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"], agenda: [[other]],
                       outcomes: [other: .skipped(other, AccountPairing.Reason.needsPhone)])
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()

        #expect(backend.paired == [other])
        #expect(model.phase == .home)
        #expect(model.servers.count == 1)
        #expect(!pairing.calls.contains { $0.hasPrefix("poll") })
        #expect(setup.phase == .ready)
    }

    @Test func syncWaitsForCall() async throws {
        let model = try await makeModel(paired: true)
        model.callHandler = StubCallHandler()
        model.startCall(AgentTarget(serverID: first.id, serverHost: "agent.example.com", agent: assistant))
        try #require(model.phase == .inCall(AgentTarget(serverID: first.id, serverHost: "agent.example.com",
                                                        agent: assistant)))
        backend.script(restore: ["acc_1"], agenda: [[other], [other]], outcomes: [other: .paired(other, homeDevice)])
        pairing.setMeResults([.success(homeInfo)], for: other)
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()
        #expect(backend.agendaCount == 1)
        #expect(backend.paired.isEmpty)

        model.callDidEnd(.normal)
        #expect(model.phase == .home)
        clock.advance(by: .seconds(60))
        await setup.becameActive()
        await setup.settle()

        #expect(backend.agendaCount == 2)
        #expect(backend.paired == [other])
        #expect(model.servers.count == 2)
    }

    @Test func concurrentActivationsSyncOnce() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        let gate = backend.holdAgenda()
        // No throttle: only the sync already running keeps the others out.
        let setup = makeSetup(model, syncInterval: 0)

        await setup.start()
        await setup.becameActive()
        await setup.becameActive()
        gate.open()
        await setup.settle()

        #expect(backend.agendaCount == 1)
    }

    @Test func manualLoginRunningSkipsTheSync() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        let setup = makeSetup(model, syncInterval: 0, isManualRunning: { true })

        await setup.start()
        await setup.becameActive()
        await setup.settle()

        #expect(backend.agendaCount == 0)
        #expect(probe.announced == ["acc_1"])
    }

    @Test func signedInIgnoresCodes() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        let setup = makeSetup(model)
        await setup.start()
        await setup.settle()

        setup.receive(code("wcc_one", account: "acc_1"))
        await setup.settle()
        #expect(backend.enrolls.isEmpty)
        #expect(setup.message == nil)
        #expect(setup.phoneAccount == "acc_1")

        setup.receive(code("wcc_two", account: "acc_2"))
        await setup.settle()
        #expect(backend.enrolls.isEmpty)
        #expect(setup.message == SetupModel.otherAccountMessage)
        #expect(setup.phoneAccount == "acc_2")
        #expect(setup.account == "acc_1")
    }

    @Test func deviceLimitShowsMessageAndStops() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.deviceLimit])
        let setup = makeSetup(model)

        await setup.start()
        await setup.settle()
        #expect(setup.message == SetupModel.deviceLimitMessage)
        #expect(backend.enrolls.count == 1)

        clock.advance(by: .seconds(30))
        await setup.settle()
        #expect(backend.enrolls.count == 1)
        #expect(clock.sleeperCount == 0)

        setup.receive(code("wcc_new"))
        await clock.waitForSleepers()
        #expect(backend.enrolls.map(\.code) == [nil, "wcc_new"])
    }

    @Test func signOutPausesAutoSetup() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1", nil])
        let setup = makeSetup(model)
        await setup.start()
        await setup.settle()

        await setup.signOut()
        #expect(setup.isPaused)
        #expect(setup.account == nil)
        #expect(backend.signOutCount == 1)
        #expect(probe.announced == ["acc_1", nil])
        #expect(defaults.bool(forKey: "setup.paused"))
        #expect(model.servers.count == 1)
        // Paused across launches.
        #expect(SetupModel(backend: backend, model: model, defaults: defaults).isPaused)

        await setup.becameActive()
        setup.receive(code("wcc_one"))
        await setup.settle()
        #expect(backend.enrolls.isEmpty)

        await setup.setUpAgain()
        await setup.settle()
        #expect(!setup.isPaused)
        #expect(defaults.object(forKey: "setup.paused") == nil)
        #expect(backend.enrolls == [.init(code: "wcc_one", tryLegacy: true)])
    }

    /// C3: signed in to acc_1, the iPhone sends an acc_2 code; "Sign out" + "Set up again" enrolls with that code.
    @Test func codeReceivedWhileSignedInIsUsedAfterSetUpAgain() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1", nil])
        let setup = makeSetup(model)
        await setup.start()
        await setup.settle()

        setup.receive(code("wcc_two", account: "acc_2"))
        await setup.settle()
        #expect(backend.enrolls.isEmpty)
        #expect(setup.message == SetupModel.otherAccountMessage)

        await setup.signOut()
        await setup.setUpAgain()
        await setup.settle()

        #expect(backend.enrolls == [.init(code: "wcc_two", tryLegacy: true)])
    }

    /// An activation while the sign out waits for the Cloud starts nothing, and the setup stays paused.
    @Test func activationDuringSignOutDoesNotReEnroll() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        backend.scriptAgenda([.success([]), .failure(AccountError.signedOut)])
        let release = backend.holdSignOut()
        // No throttle: only the pause keeps a sync out.
        let setup = makeSetup(model, syncInterval: 0)
        await setup.start()
        await setup.settle()
        #expect(backend.agendaCount == 1)

        let signingOut = Task { await setup.signOut() }
        await backend.signOutReached.wait()
        #expect(setup.isPaused)
        #expect(setup.account == nil)
        await setup.becameActive()
        setup.receive(code("wcc_one"))
        await setup.settle()
        #expect(backend.agendaCount == 1)
        #expect(backend.enrolls.isEmpty)

        release.open()
        await signingOut.value
        #expect(setup.isPaused)
        #expect(defaults.bool(forKey: "setup.paused"))
        #expect(probe.announced == ["acc_1", nil])

        await setup.becameActive()
        await setup.settle()
        #expect(backend.agendaCount == 1)
        #expect(backend.enrolls.isEmpty)
    }

    /// A loop enrollment whose first sync is still running when the device flow finishes is not announced later.
    @Test func deviceFlowSignInCancelsTheRunningSync() async throws {
        let model = try await makeModel()
        backend.script(enroll: [.enrolled(account: "acc_1")], agenda: [[other]],
                       outcomes: [other: .paired(other, homeDevice)])
        let gate = backend.holdAgenda()
        let setup = makeSetup(model)
        await setup.start()
        await backend.agendaReached.wait()

        setup.didSignIn(account: "acc_2")
        gate.open()
        await setup.settle()

        #expect(probe.announced == ["acc_2"])
        #expect(setup.account == "acc_2")
        #expect(backend.paired.isEmpty)
        #expect(setup.phase == .noAgents)
    }

    @Test func syncIsThrottled() async throws {
        let model = try await makeModel(paired: true)
        backend.script(restore: ["acc_1"])
        let setup = makeSetup(model)
        await setup.start()
        await setup.settle()
        #expect(backend.agendaCount == 1)

        clock.advance(by: .seconds(59))
        await setup.becameActive()
        await setup.settle()
        #expect(backend.agendaCount == 1)

        clock.advance(by: .seconds(1))
        await setup.becameActive()
        await setup.settle()
        #expect(backend.agendaCount == 2)
    }

    @Test func deviceFlowSignInStopsTheLoop() async throws {
        let model = try await makeModel()
        defaults.set(true, forKey: "setup.paused")
        let setup = makeSetup(model)
        #expect(setup.isPaused)
        await setup.setUpAgain()
        await clock.waitForSleepers()

        setup.didSignIn(account: "acc_1")
        #expect(setup.account == "acc_1")
        #expect(!setup.isPaused)
        #expect(defaults.object(forKey: "setup.paused") == nil)
        #expect(setup.phase == .noAgents)
        #expect(probe.announced == ["acc_1"])
        await setup.settle()

        clock.advance(by: .seconds(30))
        await setup.settle()
        #expect(backend.enrolls.count == 1)
        #expect(backend.agendaCount == 0)
    }
}
