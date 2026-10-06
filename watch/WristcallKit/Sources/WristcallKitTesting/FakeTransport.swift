import Foundation
import Synchronization
import WristcallKit

/// In-memory `CallTransport` for tests: records what the client sends and lets the
/// test play the server (`serverSends`, `serverCloses`). Behaves like `NWWebSocketTransport`:
/// sending before `connect()` or after the end throws `TransportError.notConnected`,
/// `close(code:)` ends the event stream with `.closed(code:)`.
public final class FakeTransport: CallTransport {
    /// A frame (or the close) the client handed to the transport.
    public enum Sent: Sendable, Equatable {
        case text(String)
        case binary(Data)
        case close(UInt16)
    }

    public let events: AsyncStream<TransportEvent>
    private let continuation: AsyncStream<TransportEvent>.Continuation
    private let state: Mutex<State>

    private struct State {
        var connectError: TransportError?
        var connected = false
        var ended = false
        var connectCount = 0
        var sent: [Sent] = []
    }

    /// - Parameter connectError: if set, `connect()` throws it and the stream ends with `.closed(code: nil)`.
    public init(connectError: TransportError? = nil) {
        (events, continuation) = AsyncStream.makeStream(of: TransportEvent.self, bufferingPolicy: .unbounded)
        state = Mutex(State(connectError: connectError))
    }

    // MARK: - CallTransport

    public func connect() async throws {
        let error = state.withLock { state -> TransportError? in
            state.connectCount += 1
            if state.ended || state.connected { return .notConnected }
            if let error = state.connectError {
                state.ended = true
                return error
            }
            state.connected = true
            return nil
        }
        if let error {
            finish(code: nil)
            throw error
        }
    }

    public func send(text: String) async throws {
        try record(.text(text))
    }

    public func send(binary: Data) async throws {
        try record(.binary(binary))
    }

    public func close(code: UInt16) async {
        let first = state.withLock { state -> Bool in
            guard !state.ended else { return false }
            state.ended = true
            if state.connected {
                state.sent.append(.close(code))
            }
            return true
        }
        if first {
            finish(code: code)
        }
    }

    // MARK: - Playing the server

    /// The server sends a text frame.
    public func serverSends(_ text: String) {
        guard state.withLock({ !$0.ended }) else { return }
        continuation.yield(.text(text))
    }

    /// The server sends a binary (audio) frame.
    public func serverSends(binary: Data) {
        guard state.withLock({ !$0.ended }) else { return }
        continuation.yield(.binary(binary))
    }

    /// The server closes with `code`, or the connection drops when `code` is `nil`.
    public func serverCloses(code: UInt16?) {
        let first = state.withLock { state -> Bool in
            guard !state.ended else { return false }
            state.ended = true
            return true
        }
        if first {
            finish(code: code)
        }
    }

    // MARK: - Inspection

    public var sent: [Sent] {
        state.withLock { $0.sent }
    }

    /// Text frames sent so far, in order.
    public var sentTexts: [String] {
        sent.compactMap { if case .text(let text) = $0 { text } else { nil } }
    }

    /// Binary frames sent so far, in order.
    public var sentBinaries: [Data] {
        sent.compactMap { if case .binary(let data) = $0 { data } else { nil } }
    }

    public var connectCount: Int {
        state.withLock { $0.connectCount }
    }

    public var isEnded: Bool {
        state.withLock { $0.ended }
    }

    /// Waits until `condition(sent)` holds; throws `FakeTransportTimeout` after `timeout`.
    @discardableResult
    public func waitUntilSent(
        timeout: Duration = .seconds(2),
        _ condition: @Sendable ([Sent]) -> Bool
    ) async throws -> [Sent] {
        let deadline = ContinuousClock.now + timeout
        while true {
            let current = sent
            if condition(current) { return current }
            guard ContinuousClock.now < deadline else {
                throw FakeTransportTimeout(sent: current)
            }
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    private func record(_ frame: Sent) throws {
        try state.withLock { state in
            guard state.connected, !state.ended else { throw TransportError.notConnected }
            state.sent.append(frame)
        }
    }

    private func finish(code: UInt16?) {
        continuation.yield(.closed(code: code))
        continuation.finish()
    }
}

public struct FakeTransportTimeout: Error, CustomStringConvertible {
    public let sent: [FakeTransport.Sent]
    public var description: String { "condition not met; sent so far: \(sent)" }
}
