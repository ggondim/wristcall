import Foundation
import Synchronization

/// A `CallTransport` for a call through the Wristcall Cloud entry: `connect()` first asks the Cloud for a route to
/// the agent (`CloudRouting.call(agent:)`, within `routeTimeout`), then opens the node it names with the route's
/// token, or its fallback node when the first one cannot be reached. The route is asked inside `connect()` so the
/// call keeps its order (CallKit, then audio, then the socket; TN3135).
///
/// A route the Cloud does not give ends as `TransportError.refused` with a fatal `ServerError` (`CallFailureText` has
/// its text): `plan_required`, or the local codes `no_node_available`, `route_unreachable` (no network or no
/// answer in time), `signed_out` and `route_failed`. After `connect()` the node's transport does the rest.
public final class RoutedCallTransport: CallTransport, @unchecked Sendable {
    public let events: AsyncStream<TransportEvent>

    private let continuation: AsyncStream<TransportEvent>.Continuation
    private let agent: String
    private let router: any CloudRouting
    private let routeTimeout: Duration
    private let make: @Sendable (URL, String) throws -> any CallTransport
    private let state = Mutex(State())

    private struct State {
        var phase = Phase.idle
        /// `connect()` waiting for the route (resumed once: by the route, the timeout or `close`).
        var routeWaiter: CheckedContinuation<CloudRoute, any Error>?
        var routeTasks: [Task<Void, Never>] = []
        /// The node's transport, from the moment it is made.
        var inner: (any CallTransport)?
    }

    private enum Phase {
        case idle, routing, opening, connected, closed
    }

    /// No answer from the router within `routeTimeout`.
    private struct RouteTimeout: Error {}

    /// - Parameters:
    ///   - agent: the `ag_...` id to call.
    ///   - routeTimeout: how long the route may take; the 10 s wait for `session.ready` starts after `connect()`.
    ///   - make: opens a transport to a node URL with a token (tests pass fakes).
    public init(
        agent: String,
        router: any CloudRouting,
        routeTimeout: Duration = .seconds(8),
        make: @escaping @Sendable (URL, String) throws -> any CallTransport = { try NWWebSocketTransport(server: $0, token: $1) }
    ) {
        self.agent = agent
        self.router = router
        self.routeTimeout = routeTimeout
        self.make = make
        (events, continuation) = AsyncStream.makeStream(of: TransportEvent.self, bufferingPolicy: .unbounded)
    }

    // MARK: - CallTransport

    public func connect() async throws {
        let route: CloudRoute
        do {
            route = try await fetchRoute()
        } catch TransportError.notConnected {
            throw TransportError.notConnected
        } catch is CancellationError {
            fail()
            throw CancellationError()
        } catch {
            fail()
            throw TransportError.refused(Self.refusal(for: error))
        }

        do {
            try await open(route.primary)
        } catch TransportError.connectionFailed(let reason) {
            guard let fallback = route.fallback else {
                fail()
                throw TransportError.connectionFailed(reason)
            }
            do {
                try await open(fallback)
            } catch {
                fail()
                throw error
            }
        } catch {
            fail()
            throw error
        }
    }

    public func send(text: String) async throws {
        try await connected().send(text: text)
    }

    public func send(binary: Data) async throws {
        try await connected().send(binary: binary)
    }

    public func close(code: UInt16) async {
        let (previous, waiter, tasks, inner) = state.withLock { state -> (Phase, CheckedContinuation<CloudRoute, any Error>?, [Task<Void, Never>], (any CallTransport)?) in
            let previous = state.phase
            guard previous != .closed else { return (previous, nil, [], nil) }
            state.phase = .closed
            defer {
                state.routeWaiter = nil
                state.routeTasks = []
            }
            return (previous, state.routeWaiter, state.routeTasks, state.inner)
        }
        guard previous != .closed else { return }
        tasks.forEach { $0.cancel() }
        waiter?.resume(throwing: TransportError.notConnected)
        await inner?.close(code: code)
        // Once connected the node's own `.closed` comes through the pump; before that this transport ends the stream.
        if previous != .connected {
            continuation.yield(.closed(code: code))
            continuation.finish()
        }
    }

    // MARK: - Route

    /// The route, or `TransportError.notConnected` when `close` came first. The router runs in a task of its own,
    /// so a router that ignores cancellation cannot hold the call past the timeout.
    private func fetchRoute() async throws -> CloudRoute {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { (waiter: CheckedContinuation<CloudRoute, any Error>) in
                let started = state.withLock { state -> Bool in
                    guard state.phase == .idle else { return false }
                    state.phase = .routing
                    state.routeWaiter = waiter
                    return true
                }
                guard started else {
                    waiter.resume(throwing: TransportError.notConnected)
                    return
                }
                let request = Task { [router, agent] in
                    let result: Result<CloudRoute, any Error>
                    do {
                        result = .success(try await router.call(agent: agent))
                    } catch {
                        result = .failure(error)
                    }
                    self.resolveRoute(result)
                }
                let timer = Task { [routeTimeout] in
                    try? await Task.sleep(for: routeTimeout)
                    guard !Task.isCancelled else { return }
                    self.resolveRoute(.failure(RouteTimeout()))
                }
                let settled = state.withLock { state -> Bool in
                    // The route, the timeout or `close` may have come between the two locks.
                    guard state.routeWaiter != nil else { return true }
                    state.routeTasks = [request, timer]
                    return false
                }
                if settled {
                    request.cancel()
                    timer.cancel()
                }
                // Cancelled before the handler below could find a waiter.
                if Task.isCancelled { resolveRoute(.failure(CancellationError())) }
            }
        } onCancel: {
            resolveRoute(.failure(CancellationError()))
        }
    }

    /// Hands the first outcome to `connect()` and stops the rest.
    private func resolveRoute(_ result: Result<CloudRoute, any Error>) {
        let (waiter, tasks) = state.withLock { state in
            guard state.phase == .routing, let waiter = state.routeWaiter else {
                return (nil as CheckedContinuation<CloudRoute, any Error>?, [Task<Void, Never>]())
            }
            state.routeWaiter = nil
            defer { state.routeTasks = [] }
            if case .success = result { state.phase = .opening }
            return (waiter, state.routeTasks)
        }
        tasks.forEach { $0.cancel() }
        waiter?.resume(with: result)
    }

    static func refusal(for error: any Error) -> ServerError {
        let code: ServerErrorCode
        let message: String
        switch error {
        case APIError.planRequired:
            (code, message) = (.planRequired, CallFailureText.planRequired)
        case APIError.noNodeAvailable:
            (code, message) = (.unknown(RouteFailure.noNodeAvailable), CallFailureText.noNodeAvailable)
        case APIError.network, is RouteTimeout:
            (code, message) = (.unknown(RouteFailure.unreachable), CallFailureText.unreachable)
        case AccountError.signedOut, APIError.unauthorized, APIError.revoked, APIError.forbidden:
            // `403 device_revoked`: this device no longer belongs to the account; signing in again on the iPhone fixes it.
            (code, message) = (.unknown(RouteFailure.signedOut), CallFailureText.signedOut)
        case APIError.notFound:
            (code, message) = (.unknown(RouteFailure.agentNotFound), CallFailureText.agentNotFound)
        case let error as APIError:
            (code, message) = (.unknown(RouteFailure.failed), error.message)
        default:
            (code, message) = (.unknown(RouteFailure.failed), String(describing: error))
        }
        return ServerError(code: code, message: message, fatal: true, reason: nil)
    }

    // MARK: - Node

    /// Opens the node's transport; once it is up, its events flow into `events`.
    private func open(_ target: RouteTarget) async throws {
        let inner = try make(target.url, target.token)
        let closed = state.withLock { state -> Bool in
            guard state.phase == .opening else { return true }
            state.inner = inner
            return false
        }
        if closed {
            await inner.close(code: 1000)
            throw TransportError.notConnected
        }

        do {
            try await inner.connect()
        } catch {
            if state.withLock({ $0.phase == .closed }) { throw TransportError.notConnected }
            await inner.close(code: 1000)
            throw error
        }

        let pump = state.withLock { state -> Bool in
            guard state.phase == .opening else { return false }
            state.phase = .connected
            return true
        }
        guard pump else {
            // `close` came while the node was opening: it already ended this transport's stream.
            await inner.close(code: 1000)
            throw TransportError.notConnected
        }
        Task { [continuation] in
            for await event in inner.events { continuation.yield(event) }
            continuation.finish()
        }
    }

    private func connected() throws -> any CallTransport {
        try state.withLock { state in
            guard state.phase == .connected, let inner = state.inner else { throw TransportError.notConnected }
            return inner
        }
    }

    /// `connect()` failed: the stream ends like a connection that never opened.
    private func fail() {
        let first = state.withLock { state -> Bool in
            guard state.phase != .closed else { return false }
            state.phase = .closed
            return true
        }
        guard first else { return }
        continuation.yield(.closed(code: nil))
        continuation.finish()
    }
}
