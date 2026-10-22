import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting

/// A `CloudAccess` with a fixed access token (or a fixed failure) and a `CloudClient` on a `StubHost`.
private struct FakeAccess: CloudAccess {
    let cloudClient: CloudClient
    var token: Result<String, any Error> = .success("at")

    var isSignedIn: Bool { get async { (try? token.get()) != nil } }
    func accessToken() async throws -> String { try token.get() }
}

struct CloudRouterTests {
    static let expiresAt: TimeInterval = 1_792_580_000

    static func routeJSON(node: String = "home") -> String {
        """
        {"node":{"id":"\(node)","url":"https://node-a.example.com","region":"sao"},"token":"eyJ.\(node)","expires_at":\(Int(expiresAt)),
         "fallback":null}
        """
    }

    /// A clock the test moves.
    final class Clock: Sendable {
        private let time: Mutex<TimeInterval>
        init(_ time: TimeInterval) { self.time = Mutex(time) }
        var now: Date { Date(timeIntervalSince1970: time.withLock { $0 }) }
        func set(_ value: TimeInterval) { time.withLock { $0 = value } }
    }

    func router(_ host: StubHost, clock: Clock = Clock(expiresAt - 300)) -> CloudRouter {
        let access = FakeAccess(cloudClient: CloudClient(cloud: host.url, session: .stubbed()))
        return CloudRouter(access: access, now: { clock.now })
    }

    @Test func manageRouteIsCached() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON())])
        let router = router(host)
        let first = try await router.manage()
        let second = try await router.manage()
        #expect(first == second)
        #expect(first.primary.nodeID == "home")
        #expect(host.requests.count == 1)
        let request = try #require(host.requests.first)
        #expect(request.headers["Authorization"] == "Bearer at")
        #expect(try request.json()["purpose"] as? String == "manage")
    }

    @Test func manageRouteRefreshesBeforeExpiry() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON(node: "one")), (200, Self.routeJSON(node: "two"))])
        let clock = Clock(Self.expiresAt - 300)
        let router = router(host, clock: clock)
        #expect(try await router.manage().primary.nodeID == "one")
        // 31 s before the expiry the route still counts; at 30 s it is refetched.
        clock.set(Self.expiresAt - 31)
        #expect(try await router.manage().primary.nodeID == "one")
        #expect(host.requests.count == 1)
        clock.set(Self.expiresAt - 30)
        #expect(try await router.manage().primary.nodeID == "two")
        #expect(host.requests.count == 2)
    }

    @Test func manageRouteRefreshesWithinMargin() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON(node: "one")), (200, Self.routeJSON(node: "two"))])
        let clock = Clock(Self.expiresAt - 300)
        let router = router(host, clock: clock)
        _ = try await router.manage()
        clock.set(Self.expiresAt - 29)
        #expect(try await router.manage().primary.nodeID == "two")
        #expect(host.requests.count == 2)
    }

    @Test func invalidateDropsCachedRoute() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON(node: "one")), (200, Self.routeJSON(node: "two"))])
        let router = router(host)
        #expect(try await router.manage().primary.nodeID == "one")
        await router.invalidate()
        #expect(try await router.manage().primary.nodeID == "two")
        #expect(host.requests.count == 2)
    }

    @Test func callRouteIsNeverCached() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON(node: "one")), (200, Self.routeJSON(node: "two"))])
        let router = router(host)
        #expect(try await router.call(agent: "ag_0123456789ab").primary.nodeID == "one")
        #expect(try await router.call(agent: "ag_0123456789ab").primary.nodeID == "two")
        #expect(host.requests.count == 2)
        let body = try #require(host.requests.first).json()
        #expect(body["purpose"] as? String == "call")
        #expect(body["agent"] as? String == "ag_0123456789ab")
        // A call route neither fills nor reads the manage cache.
        _ = try await router.manage()
        #expect(host.requests.count == 3)
        #expect(try #require(host.requests.last).json()["purpose"] as? String == "manage")
    }

    @Test func concurrentManageSharesOneRequest() async throws {
        let host = StubHost { _ in
            Thread.sleep(forTimeInterval: 0.3)
            return StubHost.Reply(200, Self.routeJSON())
        }
        let router = router(host)
        let routes = try await withThrowingTaskGroup(of: CloudRoute.self) { group in
            for _ in 0..<5 { group.addTask { try await router.manage() } }
            return try await group.reduce(into: []) { $0.append($1) }
        }
        #expect(routes.count == 5)
        #expect(Set(routes.map(\.primary.token)) == ["eyJ.home"])
        #expect(host.requests.count == 1)
    }

    @Test func invalidateDuringARequestKeepsItsResultOutOfTheCache() async throws {
        let host = StubHost { _ in
            Thread.sleep(forTimeInterval: 0.3)
            return StubHost.Reply(200, Self.routeJSON())
        }
        let router = router(host)
        async let first = router.manage()
        try await Task.sleep(for: .milliseconds(100))
        await router.invalidate()
        // The request in flight still answers its caller, but a later caller asks again.
        #expect(try await first.primary.nodeID == "home")
        _ = try await router.manage()
        #expect(host.requests.count == 2)
    }

    @Test func noNodeErrorPassesThrough() async throws {
        let host = StubHost(replies: [StubHost.Reply(503, #"{"error":"no_node_available","message":"x"}"#, headers: ["Retry-After": "30"])])
        let router = router(host)
        await #expect(throws: APIError.noNodeAvailable(retryAfter: 30)) { try await router.manage() }
        await #expect(throws: APIError.noNodeAvailable(retryAfter: 30)) { try await router.call(agent: "ag_0123456789ab") }
        // A failure is not cached: the next manage() asks again.
        #expect(host.requests.count == 2)
    }

    @Test func signedOutPassesThrough() async throws {
        let host = StubHost(replies: [(200, Self.routeJSON())])
        let access = FakeAccess(cloudClient: CloudClient(cloud: host.url, session: .stubbed()), token: .failure(AccountError.signedOut))
        let router = CloudRouter(access: access)
        await #expect(throws: AccountError.signedOut) { try await router.manage() }
        await #expect(throws: AccountError.signedOut) { try await router.call(agent: "ag_0123456789ab") }
        #expect(host.requests.isEmpty)
    }
}
