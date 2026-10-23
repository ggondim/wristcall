import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

/// The Wristcall Cloud entry's API: every call asks the router for the `manage` route and talks to the node it
/// names, with the node's token.
@MainActor
struct CloudEntryAPITests {
    nonisolated static func route(_ node: String = "https://node-a.example", token: String = "tok-a") -> CloudRoute {
        CloudRoute(primary: RouteTarget(
            nodeID: "node-a", url: URL(string: node)!, region: "gru", token: token,
            expiresAt: Date(timeIntervalSince1970: 1_800_000_300)))
    }

    /// The node APIs the factory made, in order, and the fakes it hands out (the last one again when they run out).
    final class Nodes: Sendable {
        private let state: Mutex<(made: [(url: URL, token: String)], fakes: [FakeServerAPI])>

        init(_ fakes: [FakeServerAPI]) { state = Mutex(([], fakes)) }

        var made: [(url: URL, token: String)] { state.withLock { $0.made } }

        var factory: ServerAPIFactory {
            { url, token in
                self.state.withLock { state in
                    state.made.append((url, token))
                    return state.fakes.count > 1 ? state.fakes.removeFirst() : state.fakes[0]
                }
            }
        }
    }

    @Test func routedAPIUsesManageRouteForAgents() async throws {
        let fake = FakeServerAPI()
        fake.agentList = [AgentDetail(id: "ag_1", slug: "a", displayName: "A")]
        let nodes = Nodes([fake])
        let router = FakeRouter { Self.route() }
        let api = RoutedServerAPI(router: router, make: nodes.factory)

        let agents = try await api.agents()

        #expect(agents.map(\.id) == ["ag_1"])
        #expect(router.manageCount == 1)
        #expect(router.agents.isEmpty)  // never a call route
        #expect(nodes.made.map(\.url) == [URL(string: "https://node-a.example")!])
        #expect(nodes.made.map(\.token) == ["tok-a"])
    }

    @Test func retriesOnceAfterNodeUnauthorized() async throws {
        let refused = FakeServerAPI()
        refused.agentsError = APIError.unauthorized
        let fine = FakeServerAPI()
        fine.agentList = [AgentDetail(id: "ag_1", slug: "a", displayName: "A")]
        let nodes = Nodes([refused, fine])
        let router = FakeRouter { Self.route() }

        let agents = try await RoutedServerAPI(router: router, make: nodes.factory).agents()

        #expect(agents.map(\.id) == ["ag_1"])
        #expect(router.invalidationCount == 1)
        #expect(router.manageCount == 2)

        // Refused twice: the error goes to the caller, with no third try.
        let again = FakeRouter { Self.route() }
        let twice = Nodes([refused])
        await #expect(throws: APIError.unauthorized) {
            try await RoutedServerAPI(router: again, make: twice.factory).agents()
        }
        #expect(again.manageCount == 2)
        #expect(twice.made.count == 2)
    }

    @Test func routeErrorsPassAsTheyCame() async throws {
        let router = FakeRouter { throw APIError.planRequired }
        let nodes = Nodes([FakeServerAPI()])

        await #expect(throws: APIError.planRequired) {
            try await RoutedServerAPI(router: router, make: nodes.factory).me()
        }
        #expect(nodes.made.isEmpty)
        #expect(router.invalidationCount == 0)
    }

    @Test func nodeOnlyRoutesThrowWithoutNetwork() async throws {
        let router = FakeRouter { Self.route() }
        let nodes = Nodes([FakeServerAPI()])
        let api = RoutedServerAPI(router: router, make: nodes.factory)

        await #expect(throws: APIError.notFound) { try await api.devices() }
        await #expect(throws: APIError.notFound) { try await api.revokeDevice("d") }
        await #expect(throws: APIError.notFound) { try await api.createPairingCode() }
        await #expect(throws: APIError.notFound) { try await api.pairingRequests() }
        await #expect(throws: APIError.notFound) { try await api.approve(requestID: "r") }
        await #expect(throws: APIError.notFound) { try await api.deny(requestID: "r") }
        await #expect(throws: APIError.notFound) { try await api.setPushKey("k") }
        await #expect(throws: APIError.notFound) { try await api.clearPushKey() }
        await #expect(throws: APIError.notFound) { try await api.linkAccount(serverToken: "t") }
        await #expect(throws: APIError.notFound) { try await api.unlinkAccount() }
        await #expect(throws: APIError.notFound) {
            try await api.linkAccount(serverToken: "t", code: try #require(PairingCode("12345678")))
        }
        #expect(router.manageCount == 0)
        #expect(nodes.made.isEmpty)
    }

    @Test func appStateDispatchesTheCloudEntry() async throws {
        let own = FakeServerAPI()
        let state = AppState(store: InMemoryManagedServerStore([]), makeAPI: { _, _ in own })
        let entry = CloudServer(id: "cs-9", name: "Wristcall Cloud", url: "https://cloud.wristcall.test", kind: "cloud",
                                linked: true, agents: [])
        state.setCloudEntry(entry)
        let cloud = try #require(state.cloudServer)

        #expect(cloud.id == ManagedServer.cloudEntryID)
        #expect(cloud.isCloudEntry)
        #expect(cloud.token.isEmpty)
        #expect(cloud.cloudServerID == "cs-9")
        #expect(cloud.linked)
        #expect(state.allServers.map(\.id) == [ManagedServer.cloudEntryID])
        #expect(state.servers.isEmpty)

        // No router yet: the entry answers that no node is there, without touching a self-hosted API.
        await #expect(throws: APIError.noNodeAvailable(retryAfter: nil)) { try await state.api(for: cloud).agents() }
        #expect(own.calls.isEmpty)

        let fake = FakeServerAPI()
        fake.agentList = [AgentDetail(id: "ag_c", slug: "c", displayName: "C")]
        state.cloudAPI = RoutedServerAPI(router: FakeRouter { Self.route() }, make: { _, _ in fake })
        #expect(try await state.api(for: cloud).agents().map(\.id) == ["ag_c"])
    }

    @Test func bundleSendsIOSClientHeader() {
        #expect(ClientIdentity.fromBundle(.main, platform: .ios)?.header == "ios/0.3.0")
    }
}
