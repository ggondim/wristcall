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
        var unpair: Result<Void, PairingError> = .success(())
        var calls: [String] = []
        var pollTokens: [String] = []
    }

    private let state = Mutex(State())

    /// One line per request, e.g. `"pair https://agent.example.com code=12345678"`. Never contains tokens.
    var calls: [String] { state.withLock { $0.calls } }
    /// The poll tokens the model sent, to check it passes the server's secret back unchanged.
    var pollTokens: [String] { state.withLock { $0.pollTokens } }

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
        try state.withLock { state in
            state.calls.append("me \(server.absoluteString)")
            return Self.next(&state.me)
        }.get()
    }

    func unpair(server: URL, token: String) async throws {
        try state.withLock { state in
            state.calls.append("unpair \(server.absoluteString)")
            return state.unpair
        }.get()
    }

    private static func next<T>(_ queue: inout [Result<T, PairingError>]) -> Result<T, PairingError> {
        queue.isEmpty ? .failure(.unexpectedStatus(599)) : queue.removeFirst()
    }
}

/// A `CredentialStore` whose `load()` always throws `error`; records whether `delete()` was called.
final class ThrowingCredentialStore: CredentialStore {
    private let error: CredentialStoreError
    private let deletes = Mutex(0)

    init(_ error: CredentialStoreError) {
        self.error = error
    }

    var deleteCount: Int { deletes.withLock { $0 } }

    func load() throws -> Credentials? { throw error }
    func save(_ credentials: Credentials) throws { throw error }
    func delete() throws { deletes.withLock { $0 += 1 } }
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
