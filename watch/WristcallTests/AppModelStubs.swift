import Foundation
import Synchronization
import WristcallKit
@testable import Wristcall

/// Plays the server and the directory for `AppModel`. Each call takes the next queued result;
/// an empty queue answers `.unexpectedStatus(599)`, so a missing setup fails loudly.
final class StubPairingService: PairingService {
    private struct State {
        var resolve: [Result<URL, PairingError>] = []
        var pair: [Result<PairResult, PairingError>] = []
        var poll: [Result<PollResult, PairingError>] = []
        var me: [Result<DeviceInfo, PairingError>] = []
        var meByServer: [URL: [Result<DeviceInfo, PairingError>]] = [:]
        var meGates: [URL: Gate] = [:]
        var unpair: Result<Void, PairingError> = .success(())
        var callStatus: [Result<CallStatus, PairingError>] = []
        var callStatusGate: Gate?
        var callStatusTokens: [String] = []
        var calls: [String] = []
        var pollTokens: [String] = []
        var unpairTokens: [String] = []
    }

    private let state = Mutex(State())

    /// One line per request, e.g. `"pair https://agent.example.com code=12345678"`. Never contains tokens.
    var calls: [String] { state.withLock { $0.calls } }
    /// The poll tokens the model sent, to check it passes the server's secret back unchanged.
    var pollTokens: [String] { state.withLock { $0.pollTokens } }
    /// The device tokens `DELETE /v1/me` revoked, to tell an old token from a new one.
    var unpairTokens: [String] { state.withLock { $0.unpairTokens } }
    var callStatusTokens: [String] { state.withLock { $0.callStatusTokens } }
    /// How many `GET /v1/calls/{id}` went out.
    var callStatusCount: Int { state.withLock { $0.calls.filter { $0.hasPrefix("callStatus ") }.count } }

    var resolveResults: [Result<URL, PairingError>] {
        get { state.withLock { $0.resolve } }
        set { state.withLock { $0.resolve = newValue } }
    }

    var pairResults: [Result<PairResult, PairingError>] {
        get { state.withLock { $0.pair } }
        set { state.withLock { $0.pair = newValue } }
    }

    var pollResults: [Result<PollResult, PairingError>] {
        get { state.withLock { $0.poll } }
        set { state.withLock { $0.poll = newValue } }
    }

    var meResults: [Result<DeviceInfo, PairingError>] {
        get { state.withLock { $0.me } }
        set { state.withLock { $0.me = newValue } }
    }

    /// `GET /v1/calls/{id}` answers, in order; once they run out, every fetch fails like a network error would.
    var callStatusResults: [Result<CallStatus, PairingError>] {
        get { state.withLock { $0.callStatus } }
        set { state.withLock { $0.callStatus = newValue } }
    }

    /// Holds every `GET /v1/calls/{id}` answer until the returned gate opens.
    func holdCallStatus() -> Gate {
        let gate = Gate()
        state.withLock { $0.callStatusGate = gate }
        return gate
    }

    /// Answers for one server, taken before `meResults`: the model asks several servers at once,
    /// so a single queue would hand out answers in whatever order the requests arrive.
    func setMeResults(_ results: [Result<DeviceInfo, PairingError>], for server: URL) {
        state.withLock { $0.meByServer[server] = results }
    }

    /// Holds every `/v1/me` answer for `server` until the returned gate opens.
    func holdMe(for server: URL) -> Gate {
        let gate = Gate()
        state.withLock { $0.meGates[server] = gate }
        return gate
    }

    var unpairResult: Result<Void, PairingError> {
        get { state.withLock { $0.unpair } }
        set { state.withLock { $0.unpair = newValue } }
    }

    func resolve(code: PairingCode, directory: URL) async throws -> URL {
        try state.withLock { state in
            state.calls.append("resolve \(code.digits) \(directory.absoluteString)")
            return Self.next(&state.resolve)
        }.get()
    }

    func pair(server: URL, code: PairingCode?, deviceName: String) async throws -> PairResult {
        try state.withLock { state in
            state.calls.append("pair \(server.absoluteString) code=\(code?.digits ?? "nil") name=\(deviceName)")
            return Self.next(&state.pair)
        }.get()
    }

    func poll(server: URL, pollToken: String) async throws -> PollResult {
        try state.withLock { state in
            state.calls.append("poll \(server.absoluteString)")
            state.pollTokens.append(pollToken)
            return Self.next(&state.poll)
        }.get()
    }

    func me(server: URL, token: String) async throws -> DeviceInfo {
        let (result, gate) = state.withLock { state in
            state.calls.append("me \(server.absoluteString)")
            let result = state.meByServer[server] != nil
                ? Self.next(&state.meByServer[server, default: []])
                : Self.next(&state.me)
            return (result, state.meGates[server])
        }
        await gate?.wait()
        return try result.get()
    }

    func unpair(server: URL, token: String) async throws {
        try state.withLock { state in
            state.calls.append("unpair \(server.absoluteString)")
            state.unpairTokens.append(token)
            return state.unpair
        }.get()
    }

    func callStatus(server: URL, token: String, callID: String) async throws -> CallStatus {
        let (result, gate) = state.withLock { state in
            state.calls.append("callStatus \(server.absoluteString) \(callID)")
            state.callStatusTokens.append(token)
            return (Self.next(&state.callStatus), state.callStatusGate)
        }
        await gate?.wait()
        return try result.get()
    }

    private static func next<T>(_ queue: inout [Result<T, PairingError>]) -> Result<T, PairingError> {
        queue.isEmpty ? .failure(.unexpectedStatus(599)) : queue.removeFirst()
    }
}

/// A `ServerStore` whose `load()` always throws `error`; records whether `deleteAll()` was called.
final class ThrowingServerStore: ServerStore {
    private let error: CredentialStoreError
    private let deletes = Mutex(0)

    init(_ error: CredentialStoreError) {
        self.error = error
    }

    var deleteCount: Int { deletes.withLock { $0 } }

    func load() throws -> [Credentials] { throw error }
    func save(_ servers: [Credentials]) throws { throw error }
    func deleteAll() throws { deletes.withLock { $0 += 1 } }
}

/// Keeps a stubbed answer waiting until the test opens it, to reorder answers or to act while a
/// request is in flight.
final class Gate: Sendable {
    private let state = Mutex<(isOpen: Bool, waiters: [CheckedContinuation<Void, Never>])>((false, []))

    func wait() async {
        await withCheckedContinuation { continuation in
            let isOpen = state.withLock { state in
                if !state.isOpen { state.waiters.append(continuation) }
                return state.isOpen
            }
            if isOpen { continuation.resume() }
        }
    }

    func open() {
        let waiters = state.withLock { state in
            state.isOpen = true
            defer { state.waiters = [] }
            return state.waiters
        }
        waiters.forEach { $0.resume() }
    }
}

/// Stands in for the CallCoordinator of task 10.
@MainActor
final class StubCallHandler: CallHandling {
    private(set) var started: [CallRequest] = []
    private(set) var endRequests = 0

    func startCall(_ request: CallRequest) {
        started.append(request)
    }

    func endCall() {
        endRequests += 1
    }
}

/// Stands in for `NetworkPathMonitor`: the test sets what the "monitor" last reported.
@MainActor
final class FakeNetworkReachability: NetworkReachability {
    var hasNetworkPath: Bool?

    init(_ hasNetworkPath: Bool?) {
        self.hasNetworkPath = hasNetworkPath
    }
}

/// Collects values from `@Sendable` closures (the injected `sleep`).
final class Recorder<Value: Sendable>: Sendable {
    private let values = Mutex<[Value]>([])

    var all: [Value] { values.withLock { $0 } }

    func append(_ value: Value) {
        values.withLock { $0.append(value) }
    }
}

/// Lets an injected closure read the model it was injected into.
@MainActor
final class ModelBox {
    var model: AppModel?
}

/// Polls `condition` on the main actor every 10 ms, for at most 2 s.
@MainActor
func waitUntil(_ condition: () -> Bool) async {
    for _ in 0..<200 where !condition() {
        try? await Task.sleep(for: .milliseconds(10))
    }
}

/// A clock for `CallStatusPoller` that never waits: each `sleep` moves it forward at once, so the
/// 3 minute deadline passes in a few milliseconds.
final class FakeClock: Sendable {
    private let start = ContinuousClock.now
    private let elapsed = Mutex(Duration.zero)

    var poller: CallStatusPoller {
        CallStatusPoller(
            sleep: { [self] duration in
                try Task.checkCancellation()
                elapsed.withLock { $0 += duration }
                await Task.yield()
            },
            now: { [self] in start + elapsed.withLock { $0 } })
    }
}
