import Foundation
import Testing
import WristcallKit
import WristcallKitTesting
@testable import WristcallPhone

@MainActor
struct AgendaSyncTests {
    nonisolated static let clock = Date(timeIntervalSince1970: 1_800_000_000)

    let world = AccountWorld()
    let home = ManagedServer(id: "home", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_h",
                             linked: true)
    let lab = ManagedServer(id: "lab", name: "Lab", url: URL(string: "https://Lab.test:443/x/")!, token: "wc_pat_l")

    var fresh: TokenSet { TokenSet(accessToken: "fresh-at", refreshToken: "rt", expiresAt: Self.clock.addingTimeInterval(3600)) }

    /// `agents`: what every server answers. Each server gets its own fake (`AppState` checks them in parallel,
    /// and a fake is not safe to share across tasks).
    func sync(_ servers: [ManagedServer], agents: [AgentDetail] = [], tokens: TokenSet? = nil)
        async -> (AgendaSync, AppState)
    {
        let session = AccountSession(cloud: world.cloud.url, kind: .ios, store: InMemoryTokenStore(tokens ?? fresh),
                                     session: .stubbed(), now: { Self.clock })
        let state = AppState(store: InMemoryManagedServerStore(servers), makeAPI: { _, _ in
            let fake = FakeServerAPI()
            fake.agentList = agents
            return fake
        })
        await state.load()
        return (AgendaSync(session: session, state: state), state)
    }

    func agents(_ count: Int) -> [AgentDetail] {
        (0..<count).map { AgentDetail(id: "ag\($0)", slug: "agent-\($0)", displayName: "Agent \($0)", icon: "waveform", callType: "one-shot") }
    }

    @Test func pushAllStoresCloudIDs() async throws {
        let (sync, state) = await sync([home, lab])

        await sync.pushAll()

        #expect(world.entries.map(\.url) == ["https://home.test", "https://lab.test/x"])
        #expect(world.entries.map(\.linked) == [true, false])
        #expect(world.entries.map(\.name) == ["Home", "Lab"])
        #expect(state.servers.map(\.cloudServerID) == ["cs-1", "cs-2"])
        #expect(sync.notice == nil)
        let post = try #require(world.cloudRequests("POST", "/v1/servers").first)
        #expect(post.headers["Authorization"] == "Bearer fresh-at")
    }

    @Test func pushAllUpdatesKnownEntriesAndReaddsMissingOnes() async throws {
        world.seed(name: "Old name", url: "https://home.test")
        var known = home
        known.cloudServerID = "cs-1"
        var gone = lab
        gone.cloudServerID = "cs-404"
        let (sync, state) = await sync([known, gone])

        await sync.pushAll()

        #expect(world.entries.map(\.name) == ["Home", "Lab"])
        #expect(world.entries.first?.linked == true)
        #expect(state.servers.map(\.cloudServerID) == ["cs-1", "cs-2"])
        #expect(world.cloudRequests("PATCH", "/v1/servers/cs-1").count == 1)
    }

    @Test func pushAllSendsAgentsOfEachServer() async throws {
        let (sync, _) = await sync([home], agents: agents(2))

        await sync.pushAll()

        #expect(world.entries.first?.agents.map { $0["slug"] } == ["agent-0", "agent-1"])
        #expect(world.entries.first?.agents.first?["call_type"] == "one-shot")
    }

    @Test func agentsSnapshotLimitedTo50() async throws {
        let (sync, state) = await sync([home])
        await sync.agentsChanged(state.servers[0], agents(60))

        let put = try #require(world.cloud.requests.first { $0.method == "PUT" })
        let sent = try #require(try put.json()["agents"] as? [[String: Any]])
        #expect(sent.count == 50)
        #expect(sent.map { $0["id"] as? String } == (0..<50).map { "ag\($0)" })
        #expect(Set(sent[0].keys) == ["id", "slug", "display_name", "icon", "call_type"])
        #expect(world.entries.first?.agents.count == 50)
    }

    @Test func longNamesAreCutToWhatTheCloudTakes() async throws {
        var named = home
        named.name = String(repeating: "é", count: 64)
        let (sync, state) = await sync([named])
        var agent = agents(1)[0]
        agent.displayName = String(repeating: "👍🏽", count: 40)
        await sync.agentsChanged(state.servers[0], [agent])

        let sentName = try #require(world.entries.first?.name)
        #expect(sentName.unicodeScalars.count <= 64)
        let sentAgent = try #require(world.entries.first?.agents.first?["display_name"])
        #expect(sentAgent.unicodeScalars.count <= 64)
        #expect(!sentAgent.isEmpty)
    }

    @Test func removeDeletesFromAgenda() async throws {
        world.seed(name: "Home", url: "https://home.test")
        var known = home
        known.cloudServerID = "cs-1"
        let (sync, _) = await sync([known])

        await sync.serverRemoved(known)
        #expect(world.entries.isEmpty)
        #expect(world.cloudRequests("DELETE", "/v1/servers/cs-1").count == 1)

        // Gone already (404) is fine; without an id, the entry is found by its URL.
        await sync.serverRemoved(known)
        world.seed(name: "Lab", url: "https://lab.test/x")
        await sync.serverRemoved(lab)
        #expect(world.entries.isEmpty)
        #expect(sync.notice == nil)
    }

    @Test func pendingSetupUsesCanonicalURL() async throws {
        world.seed(name: "Same", url: "https://SRV.test:443/")
        world.seed(name: "Other", url: "https://other.test")
        world.seed(name: "Plain", url: "http://plain.test")
        let local = ManagedServer(id: "s", name: "Srv", url: URL(string: "https://srv.test")!, token: "wc_pat_s")
        let (sync, _) = await sync([local])

        let pending = await sync.pendingSetup()

        // A plain http:// entry (the Cloud accepts any) is never offered.
        #expect(pending.map(\.name) == ["Other"])
    }

    @Test func agendaErrorIsOnlyNotice() async throws {
        world.cloudFailure = 503
        let (sync, state) = await sync([home])

        await sync.pushAll()
        await sync.serverAdded(home)
        await sync.agentsChanged(home, agents(1))

        #expect(sync.notice != nil)
        #expect(state.servers.count == 1)
        #expect(state.servers.first?.cloudServerID == nil)
        #expect(await sync.pendingSetup().isEmpty)
    }

    @Test func signedOutSyncDoesNothing() async throws {
        let session = AccountSession(cloud: world.cloud.url, kind: .ios, store: InMemoryTokenStore(),
                                     session: .stubbed(), now: { Self.clock })
        let state = AppState(store: InMemoryManagedServerStore([home]), makeAPI: { _, _ in FakeServerAPI() })
        await state.load()
        let sync = AgendaSync(session: session, state: state)

        await sync.pushAll()
        await sync.serverAdded(home)
        await sync.serverRemoved(home)

        #expect(world.cloud.requests.isEmpty)
        #expect(sync.notice == nil)
    }

    @Test func hooksMirrorAddRenameAndRemove() async throws {
        let session = AccountSession(cloud: world.cloud.url, kind: .ios, store: InMemoryTokenStore(fresh),
                                     session: .stubbed(), now: { Self.clock })
        let state = AppState(store: InMemoryManagedServerStore(), makeAPI: { _, _ in FakeServerAPI() })
        var earlierHook = 0
        state.hooks.serverAdded = { _ in earlierHook += 1 }
        let model = AccountModel(cloudURL: world.cloud.url, session: session, web: FakeWeb.approving(), state: state)
        await model.restore()

        let saved = try await state.addServer(urlText: "https://new.test", token: "wc_pat_n", name: "New")
        #expect(earlierHook == 1)  // hooks already hung stay
        #expect(world.entries.map(\.name) == ["New"])

        try state.rename(saved.id, to: "Renamed")
        try await waitUntil { world.entries.first?.name == "Renamed" }

        await state.remove(saved.id)
        #expect(world.entries.isEmpty)
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        for _ in 0..<200 where !condition() {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(condition())
    }
}
