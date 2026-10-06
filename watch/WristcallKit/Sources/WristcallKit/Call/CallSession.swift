import Foundation
import Synchronization

/// One call over protocol v1: opening, audio and mute out, server events in, end.
///
/// A `final class` with its state in a `Mutex` (not an actor): `sendAudio(_:)` and
/// `setMuted(_:)` are synchronous and cheap, so the microphone tap and the CallKit delegate
/// call them from any thread without `await`. Everything the session sends goes through one
/// ordered queue drained by a single task: `session.start` is always first, a `mute` is never
/// overtaken by audio queued before it, and `session.end` is always the last message.
public final class CallSession: Sendable {
    public static let defaultReadyTimeout: Duration = .seconds(10)
    /// How long `end()` waits for queued frames and the close frame before giving up on them.
    public static let endFlushTimeout: Duration = .seconds(2)

    /// Call events; finishes after `.ended`. Iterate it once.
    public let events: AsyncStream<CallEvent>

    private let transport: any CallTransport
    private let readyTimeout: Duration
    private let eventSink: AsyncStream<CallEvent>.Continuation
    private let outbox: AsyncStream<Outgoing>.Continuation
    private let outboxStream: AsyncStream<Outgoing>
    private let state = Mutex(State())

    private enum Outgoing: Sendable {
        case text(String)
        case binary(Data)
    }

    private enum Phase {
        case idle
        case connecting
        case starting
        case ready(SessionReady)
        case ended(CallEndReason)
    }

    private struct State {
        var phase = Phase.idle
        /// What the user wants (kept before ready, ignored after the end).
        var muted = false
        /// What the server was last told.
        var serverMuted = false
        var fatalError: ServerError?
        var readyWaiter: CheckedContinuation<SessionReady, any Error>?
        var sender: Task<Void, Never>?
        var timeout: Task<Void, Never>?

        var isEnded: Bool {
            if case .ended = phase { true } else { false }
        }

        var isReady: Bool {
            if case .ready = phase { true } else { false }
        }

        /// Marks the call ended; returns the pending `start` waiter, if any.
        mutating func end(_ reason: CallEndReason) -> CheckedContinuation<SessionReady, any Error>? {
            phase = .ended(reason)
            timeout?.cancel()
            defer { readyWaiter = nil }
            return readyWaiter
        }
    }

    public init(transport: any CallTransport, readyTimeout: Duration = CallSession.defaultReadyTimeout) {
        self.transport = transport
        self.readyTimeout = readyTimeout
        (events, eventSink) = AsyncStream.makeStream(of: CallEvent.self, bufferingPolicy: .unbounded)
        (outboxStream, outbox) = AsyncStream.makeStream(of: Outgoing.self, bufferingPolicy: .unbounded)
    }

    /// Format of the agent's audio; `nil` until `session.ready`.
    public var audioOut: AudioFormat? {
        state.withLock { state in
            if case .ready(let ready) = state.phase { ready.audioOut } else { nil }
        }
    }

    /// The mute state the user asked for.
    public var isMuted: Bool {
        state.withLock { $0.muted }
    }

    // MARK: - Opening

    /// Connects, sends `session.start` and waits for `session.ready`.
    ///
    /// Throws `CallSessionError`: `.timedOut` (no ready within the timeout; the call ends with
    /// `.connectionLost`), `.ended(reason)` (4401, fatal opening error, connection failure, or
    /// `end()` while opening), `.alreadyStarted`. Every failure also emits `.ended`.
    @discardableResult
    public func start(profile: String? = nil) async throws -> SessionReady {
        try state.withLock { state in
            switch state.phase {
            case .idle:
                state.phase = .connecting
            case .ended(let reason):
                throw CallSessionError.ended(reason)
            case .connecting, .starting, .ready:
                throw CallSessionError.alreadyStarted
            }
        }

        do {
            try await transport.connect()
        } catch {
            finish(.connectionLost)
            throw CallSessionError.ended(endReason ?? .connectionLost)
        }

        let startText = try ClientMessage.sessionStart(profile: profile).jsonText()
        return try await withCheckedThrowingContinuation { waiter in
            let endedEarly = state.withLock { state -> CallEndReason? in
                guard case .connecting = state.phase else {
                    // end() was called while connecting.
                    if case .ended(let reason) = state.phase { return reason }
                    return .normal
                }
                state.phase = .starting
                state.readyWaiter = waiter
                outbox.yield(.text(startText))
                state.sender = Task { await self.drainOutbox() }
                state.timeout = Task { [readyTimeout] in
                    try? await Task.sleep(for: readyTimeout)
                    guard !Task.isCancelled else { return }
                    await self.readyTimedOut()
                }
                return nil
            }
            if let endedEarly {
                waiter.resume(throwing: CallSessionError.ended(endedEarly))
                return
            }
            Task { await self.receive() }
        }
    }

    // MARK: - Audio and mute

    /// Queues one frame of microphone audio (PCM16 LE mono 16 kHz; 640 bytes for 20 ms).
    /// Dropped before `session.ready`, while muted and after the end.
    public func sendAudio(_ frame: Data) {
        state.withLock { state in
            guard state.isReady, !state.muted else { return }
            outbox.yield(.binary(frame))
        }
    }

    /// Mutes or unmutes. Before `session.ready` the value is kept and, if muted, `mute` is
    /// sent right after ready. After the end it is ignored. Repeating a value sends nothing.
    public func setMuted(_ muted: Bool) {
        state.withLock { state in
            guard !state.isEnded else { return }
            state.muted = muted
            guard state.isReady, state.serverMuted != muted else { return }
            state.serverMuted = muted
            outbox.yield(.text(Self.muteText(muted)))
        }
    }

    // MARK: - End

    /// Hangs up: sends `session.end` (if `session.start` went out), closes with 1000 and
    /// emits `.ended(.normal)`. Waits at most `endFlushTimeout` for queued frames to go out,
    /// even if a send is stuck on the network, then closes (the transport bounds its own close).
    /// Events arriving after this call are dropped. Does nothing if the call already ended.
    public func end() async {
        let snapshot = state.withLock { state -> (CheckedContinuation<SessionReady, any Error>?, Task<Void, Never>?, Bool)? in
            let opened: Bool
            switch state.phase {
            case .ended:
                return nil
            case .idle, .connecting:
                opened = false
            case .starting, .ready:
                opened = true
            }
            let waiter = state.end(.normal)
            return (waiter, state.sender, opened)
        }
        guard case let (waiter, sender, opened)? = snapshot else { return }

        if opened {
            outbox.yield(.text(Self.sessionEndText))
        }
        outbox.finish()
        // Close only after everything queued (session.end last) went out, or after the limit:
        // a stuck send must not hold the call (the close then tears the connection down).
        if let sender {
            await Self.wait(for: sender, atMost: Self.endFlushTimeout)
        }
        await transport.close(code: CloseCode.normal.rawValue)
        waiter?.resume(throwing: CallSessionError.ended(.normal))
        emitEnded(.normal)
    }

    // MARK: - Internals

    private static let sessionEndText = (try? ClientMessage.sessionEnd.jsonText()) ?? #"{"type":"session.end"}"#

    private static func muteText(_ muted: Bool) -> String {
        (try? ClientMessage.mute(muted).jsonText()) ?? #"{"muted":\#(muted),"type":"mute"}"#
    }

    private var endReason: CallEndReason? {
        state.withLock { state in
            if case .ended(let reason) = state.phase { reason } else { nil }
        }
    }

    private func drainOutbox() async {
        for await item in outboxStream {
            switch item {
            case .text(let text):
                try? await transport.send(text: text)
            case .binary(let data):
                try? await transport.send(binary: data)
            }
        }
    }

    private func receive() async {
        for await event in transport.events {
            switch event {
            case .text(let text):
                handle(text: text)
            case .binary(let data):
                emit(.agentAudio(data), onlyWhenReady: true)
            case .closed(let code):
                finish(reason(forClose: code))
            }
        }
    }

    private func handle(text: String) {
        // Malformed messages and unknown types are ignored (protocol v1 compatibility).
        guard let message = try? ServerMessage.decode(text) else { return }
        switch message {
        case .sessionReady(let ready):
            let waiter = state.withLock { state -> CheckedContinuation<SessionReady, any Error>? in
                guard case .starting = state.phase else { return nil }
                state.phase = .ready(ready)
                state.timeout?.cancel()
                if state.muted {
                    state.serverMuted = true
                    outbox.yield(.text(Self.muteText(true)))
                }
                defer { state.readyWaiter = nil }
                return state.readyWaiter
            }
            waiter?.resume(returning: ready)
        case .userTurnEnded(let reason):
            emit(.userTurnEnded(reason))
        case .transcript(let transcript):
            emit(.transcript(role: transcript.role, text: transcript.text))
        case .agentTurnStarted:
            emit(.agentTurnStarted)
        case .agentTurnEnded:
            emit(.agentTurnEnded)
        case .error(let error):
            if error.fatal {
                state.withLock { $0.fatalError = error }
            }
            emit(.error(code: error.code, message: error.message, fatal: error.fatal))
        case .unknown:
            break
        }
    }

    private func reason(forClose code: UInt16?) -> CallEndReason {
        if code == CloseCode.unauthorized.rawValue {
            return .unauthorized
        }
        if let fatal = state.withLock({ $0.fatalError }) {
            return .serverFatal(fatal.code)
        }
        switch code {
        case CloseCode.protocolError.rawValue:
            return .serverFatal(nil)
        case CloseCode.normal.rawValue:
            return .normal
        default:
            // No close frame (network lost) or any other code (1001 going away, 1011 ping timeout...).
            return .connectionLost
        }
    }

    private func readyTimedOut() async {
        let timedOut = finish(.connectionLost, waiterError: .timedOut, onlyWhileStarting: true)
        if timedOut {
            await transport.close(code: CloseCode.normal.rawValue)
        }
    }

    /// Ends the call for a reason that did not come from `end()`. Returns `false` if it had
    /// already ended (or, with `onlyWhileStarting`, if it was no longer opening).
    @discardableResult
    private func finish(
        _ reason: CallEndReason,
        waiterError: CallSessionError? = nil,
        onlyWhileStarting: Bool = false
    ) -> Bool {
        let result = state.withLock { state -> (Bool, CheckedContinuation<SessionReady, any Error>?) in
            if state.isEnded { return (false, nil) }
            if onlyWhileStarting {
                guard case .starting = state.phase else { return (false, nil) }
            }
            return (true, state.end(reason))
        }
        guard result.0 else { return false }
        outbox.finish()
        result.1?.resume(throwing: waiterError ?? CallSessionError.ended(reason))
        emitEnded(reason)
        return true
    }

    /// Yields under the lock, so nothing can be yielded after `.ended`.
    private func emit(_ event: CallEvent, onlyWhenReady: Bool = false) {
        state.withLock { state in
            guard !state.isEnded, !onlyWhenReady || state.isReady else { return }
            eventSink.yield(event)
        }
    }

    private func emitEnded(_ reason: CallEndReason) {
        eventSink.yield(.ended(reason))
        eventSink.finish()
    }

    /// Waits for `task` to finish, but returns after `limit` even if it has not.
    ///
    /// `await task.value` ignores cancellation, so a task group racing it against a sleep
    /// could not finish while `task` is stuck in a network send. Here two unstructured tasks
    /// race to open a one-shot gate instead, and the loser is simply left behind. Returns
    /// whether `task` finished in time; on timeout `task` is cancelled.
    @discardableResult
    private static func wait(for task: Task<Void, Never>, atMost limit: Duration) async -> Bool {
        let gate = FirstOfGate()
        let finished = Task {
            await task.value
            gate.open(finished: true)
        }
        let timer = Task {
            try? await Task.sleep(for: limit)
            gate.open(finished: false)
        }
        let inTime = await gate.wait()
        timer.cancel()
        if !inTime {
            finished.cancel()
            task.cancel()
        }
        return inTime
    }
}

/// A gate opened once by whichever side gets there first; `wait()` returns its value.
private final class FirstOfGate: Sendable {
    private enum Slot {
        case closed
        case waiting(CheckedContinuation<Bool, Never>)
        case open(Bool)
    }

    private let slot = Mutex(Slot.closed)

    /// Opens the gate with `value`. Only the first call counts.
    func open(finished value: Bool) {
        let waiter = slot.withLock { slot -> CheckedContinuation<Bool, Never>? in
            switch slot {
            case .open:
                return nil
            case .closed:
                slot = .open(value)
                return nil
            case .waiting(let waiter):
                slot = .open(value)
                return waiter
            }
        }
        waiter?.resume(returning: value)
    }

    /// Suspends until the gate opens. Call at most once.
    func wait() async -> Bool {
        await withCheckedContinuation { waiter in
            let value = slot.withLock { slot -> Bool? in
                if case .open(let value) = slot { return value }
                slot = .waiting(waiter)
                return nil
            }
            if let value {
                waiter.resume(returning: value)
            }
        }
    }
}
