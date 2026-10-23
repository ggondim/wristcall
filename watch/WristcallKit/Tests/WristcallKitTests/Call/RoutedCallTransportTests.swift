import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting

struct RoutedCallTransportTests {
    static func target(_ node: String) -> RouteTarget {
        RouteTarget(nodeID: node, url: URL(string: "https://\(node).example")!, region: "sao",
                    token: "tok-\(node.suffix(1))", expiresAt: Date(timeIntervalSince1970: 1_792_580_000))
    }

    static let route = CloudRoute(primary: target("node-a"), fallback: target("node-b"))

    /// Records what `make` was given and hands out the transports in order.
    final class Maker: Sendable {
        private let state: Mutex<(made: [(URL, String)], transports: [FakeTransport])>

        init(_ transports: [FakeTransport]) {
            state = Mutex(([], transports))
        }

        var made: [(URL, String)] { state.withLock { $0.made } }

        func make(_ url: URL, _ token: String) throws -> any CallTransport {
            try state.withLock { state in
                state.made.append((url, token))
                guard !state.transports.isEmpty else { throw TransportError.connectionFailed("no transport left") }
                return state.transports.removeFirst()
            }
        }
    }

    static func refusal(of transport: RoutedCallTransport) async -> ServerError? {
        do {
            try await transport.connect()
            return nil
        } catch TransportError.refused(let error) {
            return error
        } catch {
            Issue.record("unexpected error \(error)")
            return nil
        }
    }

    static func eventually(_ condition: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(2)
        while !condition() {
            guard ContinuousClock.now < deadline else { throw TransportError.notConnected }
            try await Task.sleep(for: .milliseconds(5))
        }
    }

    @Test func routedTransportOpensNodeWithRouteToken() async throws {
        let node = FakeTransport()
        let maker = Maker([node])
        let router = FakeRouter { Self.route }
        let transport = RoutedCallTransport(agent: "ag_1", router: router, make: maker.make)

        try await transport.connect()

        #expect(router.agents == ["ag_1"])
        #expect(maker.made.map(\.0) == [URL(string: "https://node-a.example")!])
        #expect(maker.made.map(\.1) == ["tok-a"])
        #expect(node.connectCount == 1)

        // Sends go to the node, its events come out of the routed transport.
        try await transport.send(text: "hello")
        #expect(node.sentTexts == ["hello"])
        node.serverSends("ready")
        await transport.close(code: 1000)
        #expect(node.sent.last == .close(1000))
        var events: [TransportEvent] = []
        for await event in transport.events { events.append(event) }
        #expect(events == [.text("ready"), .closed(code: 1000)])
    }

    @Test func routedTransportFallsBackOnConnectFailure() async throws {
        let failing = FakeTransport(connectError: .connectionFailed("refused"))
        let fallback = FakeTransport()
        let maker = Maker([failing, fallback])
        let transport = RoutedCallTransport(agent: "ag_1", router: FakeRouter { Self.route }, make: maker.make)

        try await transport.connect()

        #expect(maker.made.map(\.0) == [URL(string: "https://node-a.example")!, URL(string: "https://node-b.example")!])
        #expect(maker.made.map(\.1) == ["tok-a", "tok-b"])
        #expect(fallback.connectCount == 1)
        // The failed primary's `.closed(nil)` is not forwarded: the call goes on through the fallback.
        fallback.serverSends("ready")
        fallback.serverCloses(code: 1000)
        var events: [TransportEvent] = []
        for await event in transport.events { events.append(event) }
        #expect(events == [.text("ready"), .closed(code: 1000)])
    }

    @Test func routeUnavailableRefusesConnect() async throws {
        let maker = Maker([FakeTransport()])
        let router = FakeRouter { throw APIError.noNodeAvailable(retryAfter: 30) }
        let transport = RoutedCallTransport(agent: "ag_1", router: router, make: maker.make)

        let refusal = try #require(await Self.refusal(of: transport))

        #expect(refusal.code == .unknown("no_node_available"))
        #expect(refusal.fatal)
        #expect(refusal.reason == nil)
        #expect(maker.made.isEmpty)
    }

    @Test func routeTimeoutRefusesAsUnreachable() async throws {
        let transport = RoutedCallTransport(agent: "ag_1", router: FakeRouter.hanging(), routeTimeout: .milliseconds(50),
                                            make: Maker([]).make)
        let started = ContinuousClock.now

        let refusal = try #require(await Self.refusal(of: transport))

        #expect(refusal.code == .unknown("route_unreachable"))
        #expect(refusal.fatal)
        #expect(ContinuousClock.now - started < .seconds(2))
    }

    @Test func closeDuringRouteFetchEndsCall() async throws {
        let router = FakeRouter.hanging()
        let maker = Maker([FakeTransport()])
        let transport = RoutedCallTransport(agent: "ag_1", router: router, make: maker.make)

        let connecting = Task { try await transport.connect() }
        try await Self.eventually { router.agents == ["ag_1"] }
        await transport.close(code: 1000)

        await #expect(throws: TransportError.notConnected) { try await connecting.value }
        var events: [TransportEvent] = []
        for await event in transport.events { events.append(event) }
        #expect(events == [.closed(code: 1000)])
        #expect(maker.made.isEmpty)
        try await Self.eventually { router.wasCancelled }
        await #expect(throws: TransportError.notConnected) { try await transport.send(text: "late") }
    }

    /// What the Cloud's route answers for a revoked device and an agent it does not have, as they reach the watch.
    @Test func routeForbiddenAndNotFoundRepliesMapToTheirTexts() async throws {
        let revoked = APIError.error(status: 403, data: Data(#"{"error":"device_revoked","message":"device revoked"}"#.utf8))
        let missing = APIError.error(status: 404, data: Data(#"{"error":"not_found","message":"no such agent"}"#.utf8))
        let expected = [
            (revoked, "Open wristcall on your iPhone to sign in again."),
            (missing, "Agent not found."),
        ]
        for (error, text) in expected {
            let transport = RoutedCallTransport(agent: "ag_1", router: FakeRouter { throw error }, make: Maker([]).make)
            let refusal = try #require(await Self.refusal(of: transport))
            #expect(CallFailureText.text(for: refusal) == text)
            #expect(refusal.message == text)
        }
    }

    @Test func planRequiredRouteMapsToPlanText() async throws {
        let transport = RoutedCallTransport(agent: "ag_1", router: FakeRouter { throw APIError.planRequired },
                                            make: Maker([]).make)

        let refusal = try #require(await Self.refusal(of: transport))

        #expect(refusal.code == .planRequired)
        #expect(CallFailureText.text(for: refusal) == "This account has no Wristcall Cloud plan.")
    }

    @Test(arguments: [
        (APIError.network(.notConnectedToInternet) as any Error, "route_unreachable"),
        (AccountError.signedOut, "signed_out"),
        (APIError.unauthorized, "signed_out"),
        (APIError.revoked, "signed_out"),
        (APIError.forbidden("device revoked"), "signed_out"),
        (APIError.notFound, "agent_not_found"),
        (APIError.unexpectedStatus(500), "route_failed"),
    ])
    func routeErrorsMapToLocalCodes(error: any Error, code: String) async throws {
        let transport = RoutedCallTransport(agent: "ag_1", router: FakeRouter { throw error }, make: Maker([]).make)

        let refusal = try #require(await Self.refusal(of: transport))

        #expect(refusal.code == .unknown(code))
        #expect(refusal.fatal)
        #expect(refusal.reason == nil)
        #expect(CallFailureText.text(for: refusal) == [
            "route_unreachable": "Can't reach Wristcall Cloud.",
            "signed_out": "Open wristcall on your iPhone to sign in again.",
            "agent_not_found": "Agent not found.",
        ][code])
        if code == "route_failed" {
            #expect(refusal.message == APIError.unexpectedStatus(500).message)
        }
    }
}
