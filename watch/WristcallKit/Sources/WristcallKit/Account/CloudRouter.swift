import Foundation

/// What the apps ask for routes through: `CloudRouter` in the apps, a fake in tests (the watch's tests cannot
/// stub the network).
public protocol CloudRouting: Sendable {
    /// A route for one call to `agent` (an `ag_...` id).
    func call(agent: String) async throws -> CloudRoute
    /// The route to read and change the account's cloud-mode agents and history.
    func manage() async throws -> CloudRoute
    /// Drops a cached `manage` route (for example after the node answered `401`).
    func invalidate() async
}

/// Asks the Cloud which node serves the account. A `call` route is asked for every call and never kept; the
/// `manage` route is kept until 30 seconds before its token expires, and callers that ask at the same time share
/// one request. Errors (`APIError`, `AccountError.signedOut`) pass as they came.
public actor CloudRouter: CloudRouting {
    /// A cached `manage` route is dropped this long before its token expires.
    static let margin: TimeInterval = 30

    private let access: any CloudAccess
    private let now: @Sendable () -> Date
    private var cached: CloudRoute?
    private var inFlight: Task<CloudRoute, any Error>?
    /// Bumped by `invalidate()`: a request started before it does not fill the cache.
    private var generation = 0

    public init(access: any CloudAccess, now: @escaping @Sendable () -> Date = Date.init) {
        self.access = access
        self.now = now
    }

    /// A route for one call to `agent` (an `ag_...` id). Never cached.
    public func call(agent: String) async throws -> CloudRoute {
        try await fetch(.call, agent: agent)
    }

    /// The route to read and change the account's cloud-mode agents and history.
    public func manage() async throws -> CloudRoute {
        if let cached, now() < cached.primary.expiresAt.addingTimeInterval(-Self.margin) { return cached }
        if let inFlight { return try await inFlight.value }
        let started = generation
        let task = Task { [self] in
            do {
                let route = try await fetch(.manage, agent: nil)
                finish(route, started: started)
                return route
            } catch {
                finish(nil, started: started)
                throw error
            }
        }
        inFlight = task
        return try await task.value
    }

    /// Drops the cached `manage` route (for example after a node answered `401`). A request already in flight is
    /// not cancelled: the callers waiting for it get its answer, but it is not cached and the next caller asks again.
    public func invalidate() {
        generation += 1
        cached = nil
        inFlight = nil
    }

    private func finish(_ route: CloudRoute?, started: Int) {
        guard generation == started else { return }
        cached = route
        inFlight = nil
    }

    private func fetch(_ purpose: RoutePurpose, agent: String?) async throws -> CloudRoute {
        try await access.cloudClient.route(purpose: purpose, agent: agent, accessToken: try await access.accessToken())
    }
}
