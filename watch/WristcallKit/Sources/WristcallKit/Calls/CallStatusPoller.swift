import Foundation

/// Asks for a one-way call's status after hang-up until it is final (`docs/protocol.md`, "One-way calls").
///
/// The first fetch goes out at once, the next ones every `interval`. A network error or an unexpected
/// reply only waits for the next turn: the watch often drops Wi-Fi for a moment. It gives up after
/// `timeout` (a one-shot normally ends within about 75 s) and on Task cancellation.
public struct CallStatusPoller: Sendable {
    public typealias Sleep = @Sendable (Duration) async throws -> Void
    public typealias Now = @Sendable () -> ContinuousClock.Instant

    /// The protocol asks clients to poll every 1 to 2 s.
    public static let defaultInterval: Duration = .milliseconds(1500)
    /// Beyond the ~75 s a one-shot takes at most with the default timeouts, with room to spare.
    public static let defaultTimeout: Duration = .seconds(180)

    /// How a run ended.
    public enum Outcome: Sendable, Equatable {
        /// The server reached a final state.
        case final(CallStatus)
        /// The deadline passed; the last status seen, if any answer came at all.
        case timedOut(CallStatus?)
        /// `404`: no such call for this device (or it left the history).
        case notFound
        /// `401`: the device token is no longer valid.
        case unauthorized
        case cancelled
    }

    public let interval: Duration
    public let timeout: Duration
    private let sleep: Sleep
    private let now: Now

    /// - Parameters:
    ///   - sleep: waits between fetches; tests pass one that only moves a fake clock.
    ///   - now: a monotonic clock, so a change of the watch's time cannot cut or stretch the deadline.
    public init(
        interval: Duration = CallStatusPoller.defaultInterval,
        timeout: Duration = CallStatusPoller.defaultTimeout,
        sleep: @escaping Sleep = { try await Task.sleep(for: $0) },
        now: @escaping Now = { .now }
    ) {
        self.interval = interval
        self.timeout = timeout
        self.sleep = sleep
        self.now = now
    }

    /// Fetches until a final state, the deadline or cancellation.
    ///
    /// - Parameters:
    ///   - fetch: one request, usually `PairingClient.callStatus(server:token:callID:)`.
    ///   - onUpdate: every status received, the final one included.
    public func run(
        fetch: @Sendable () async throws -> CallStatus,
        onUpdate: @Sendable (CallStatus) async -> Void
    ) async -> Outcome {
        let deadline = now() + timeout
        var last: CallStatus?
        while true {
            if Task.isCancelled { return .cancelled }
            do {
                let status = try await fetch()
                last = status
                await onUpdate(status)
                if status.state.isFinal { return .final(status) }
            } catch PairingError.notFound {
                return .notFound
            } catch PairingError.unauthorized {
                return .unauthorized
            } catch {
                // URLSession reports a cancelled task as a network error: not worth another try.
                if Task.isCancelled { return .cancelled }
            }
            if now() >= deadline { return .timedOut(last) }
            do {
                try await sleep(interval)
            } catch {
                return .cancelled
            }
        }
    }
}
