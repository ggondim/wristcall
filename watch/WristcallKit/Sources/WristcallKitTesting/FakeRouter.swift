import Foundation
import Synchronization
import WristcallKit

/// A `CloudRouting` double: every `call(agent:)` runs `answer`, every `manage()` runs `manage` (or `answer`
/// without one), and it records the agents asked for, the `manage()` calls and the `invalidate()` calls.
public final class FakeRouter: CloudRouting {
    private struct Log {
        var agents: [String] = []
        var cancelled = false
        var manages = 0
        var invalidations = 0
    }

    private let answer: @Sendable () async throws -> CloudRoute
    private let manageAnswer: @Sendable () async throws -> CloudRoute
    private let log = Mutex(Log())

    public init(
        _ answer: @escaping @Sendable () async throws -> CloudRoute,
        manage: (@Sendable () async throws -> CloudRoute)? = nil
    ) {
        self.answer = answer
        manageAnswer = manage ?? answer
    }

    /// A router whose answer never comes (until the request is cancelled).
    public static func hanging() -> FakeRouter {
        FakeRouter { try await Task.sleep(for: .seconds(60)); throw CancellationError() }
    }

    public var agents: [String] { log.withLock { $0.agents } }
    public var wasCancelled: Bool { log.withLock { $0.cancelled } }
    /// How many `manage()` calls came.
    public var manageCount: Int { log.withLock { $0.manages } }
    /// How many `invalidate()` calls came.
    public var invalidationCount: Int { log.withLock { $0.invalidations } }

    public func call(agent: String) async throws -> CloudRoute {
        log.withLock { $0.agents.append(agent) }
        do {
            return try await answer()
        } catch is CancellationError {
            log.withLock { $0.cancelled = true }
            throw CancellationError()
        }
    }

    public func manage() async throws -> CloudRoute {
        log.withLock { $0.manages += 1 }
        return try await manageAnswer()
    }

    public func invalidate() async {
        log.withLock { $0.invalidations += 1 }
    }
}
