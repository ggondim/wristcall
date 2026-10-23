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

    /// The device session of account `acc_1`, fresh for an hour.
    func deviceSession(_ stored: StoredDeviceSession? = nil) -> DeviceSession {
        world.addAccount("acc_1")
        return DeviceSession(cloud: world.cloud.url, store: InMemoryDeviceSessionStore(stored ?? world.storedSession("acc_1")),
                             session: .stubbed(), now: { Self.clock })
    }

    /// The app's account on this world: `acc_1`, started (servers read, launch mirror done).
    func accountModel(_ servers: [ManagedServer], makeAPI: @escaping ServerAPIFactory) async -> AccountModel {
        world.addAccount("acc_1")
        let rig = AccountRig(world, session: world.storedSession("acc_1"), servers: servers, makeAPI: makeAPI)
        return await rig.started()
    }

    /// `agents`: what every server answers. Each server gets its own fake (`AppState` checks them in parallel,
    /// and a fake is not safe to share across tasks).
    func sync(_ servers: [ManagedServer], agents: [AgentDetail] = []) async -> (AgendaSync, AppState) {
        let session = deviceSession()
        let state = AppState(store: InMemoryManagedServerStore(servers), makeAPI: { _, _ in
            let fake = FakeServerAPI()
            fake.agentList = agents
            return fake
        })
        await state.load()
        return (AgendaSync(session: session, state: state), state)
    }

    @Test func snapshotCopiesTheAgentColor() {
        let colored = AgentDetail(id: "ag1", slug: "a", displayName: "A", color: .indigo)
        let plain = AgentDetail(id: "ag2", slug: "b", displayName: "B")
        #expect(AgendaSync.snapshot([colored, plain]).map(\.color) == [.indigo, nil])
    }

    func agents(_ count: Int) -> [AgentDetail] {
        (0..<count).map { AgentDetail(id: "ag\($0)", slug: "agent-\($0)", displayName: "Agent \($0)", icon: "waveform", callType: "one-shot") }
    }

    @Test func pushAllStoresCloudIDs() async throws {
        let (sync, state) = await sync([home, lab])

        sync.pushAll()

        await sync.idle()

        #expect(world.entries.map(\.url) == ["https://home.test", "https://lab.test/x"])
        #expect(world.entries.map(\.linked) == [true, false])
        #expect(world.entries.map(\.name) == ["Home", "Lab"])
        #expect(state.servers.map(\.cloudServerID) == ["cs-1", "cs-2"])
        #expect(sync.notice == nil)
        let post = try #require(world.cloudRequests("POST", "/v1/servers").first)
        #expect(post.headers["Authorization"] == "Bearer at-acc_1-0")
        // Every Cloud request carries the device session's token, never another one.
        #expect(world.cloud.requests.allSatisfy { $0.headers["Authorization"] == "Bearer at-acc_1-0" })
    }

    @Test func pushAllUpdatesKnownEntriesAndReaddsMissingOnes() async throws {
        world.seed(name: "Old name", url: "https://home.test")
        var known = home
        known.cloudServerID = "cs-1"
        var gone = lab
        gone.cloudServerID = "cs-404"
        let (sync, state) = await sync([known, gone])

        sync.pushAll()

        await sync.idle()

        #expect(world.entries.map(\.name) == ["Home", "Lab"])
        #expect(world.entries.first?.linked == true)
        #expect(state.servers.map(\.cloudServerID) == ["cs-1", "cs-2"])
        #expect(world.cloudRequests("PATCH", "/v1/servers/cs-1").count == 1)
    }

    @Test func pushAllSendsAgentsOfEachServer() async throws {
        let (sync, _) = await sync([home], agents: agents(2))

        sync.pushAll()

        await sync.idle()

        #expect(world.entries.first?.agents.map { $0["slug"] } == ["agent-0", "agent-1"])
        #expect(world.entries.first?.agents.first?["call_type"] == "one-shot")
    }

    @Test func agentsSnapshotLimitedTo50() async throws {
        let (sync, state) = await sync([home])
        sync.agentsChanged(state.servers[0], agents(60))
        await sync.idle()

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
        sync.agentsChanged(state.servers[0], [agent])
        await sync.idle()

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

        sync.serverRemoved(known)

        await sync.idle()
        #expect(world.entries.isEmpty)
        #expect(world.cloudRequests("DELETE", "/v1/servers/cs-1").count == 1)

        // Gone already (404) is fine; without an id, the entry is found by its URL.
        sync.serverRemoved(known)
        await sync.idle()
        world.seed(name: "Lab", url: "https://lab.test/x")
        sync.serverRemoved(lab)
        await sync.idle()
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

        sync.pushAll()

        await sync.idle()
        sync.serverAdded(home)
        await sync.idle()
        sync.agentsChanged(home, agents(1))
        await sync.idle()

        #expect(sync.notice != nil)
        #expect(state.servers.count == 1)
        #expect(state.servers.first?.cloudServerID == nil)
        #expect(await sync.pendingSetup().isEmpty)
    }

    @Test func signedOutSyncDoesNothing() async throws {
        let session = DeviceSession(cloud: world.cloud.url, store: InMemoryDeviceSessionStore(), session: .stubbed(),
                                    now: { Self.clock })
        let state = AppState(store: InMemoryManagedServerStore([home]), makeAPI: { _, _ in FakeServerAPI() })
        await state.load()
        let sync = AgendaSync(session: session, state: state)

        sync.pushAll()

        await sync.idle()
        sync.serverAdded(home)
        await sync.idle()
        sync.serverRemoved(home)
        await sync.idle()

        #expect(world.cloud.requests.isEmpty)
        #expect(sync.notice == nil)
    }

    @Test func hooksMirrorAddRenameAndRemove() async throws {
        world.addAccount("acc_1")
        var earlierHook = 0
        let rig = AccountRig(world, session: world.storedSession("acc_1"), configure: { state in
            state.hooks.serverAdded = { _ in earlierHook += 1 }
        })
        let model = await rig.started()
        let state = model.appState

        let saved = try await state.addServer(urlText: "https://new.test", token: "wc_pat_n", name: "New")
        #expect(earlierHook == 1)  // hooks already hung stay
        await model.agenda?.idle()
        #expect(world.entries.map(\.name) == ["New"])

        try state.rename(saved.id, to: "Renamed")
        try await waitUntil { world.entries.first?.name == "Renamed" }

        await state.remove(saved.id)
        await model.agenda?.idle()
        #expect(world.entries.isEmpty)
    }

    // MARK: - Background queue

    func accountModel(_ servers: [ManagedServer]) async -> AccountModel {
        await accountModel(servers) { _, _ in
            let fake = FakeServerAPI()
            fake.agentList = (0..<2).map { AgentDetail(id: "ag\($0)", slug: "agent-\($0)", displayName: "Agent \($0)") }
            return fake
        }
    }

    @Test func hungCloudDoesNotDelayServerChanges() async throws {
        let model = await accountModel([])
        let state = model.appState
        world.hold()
        defer { world.release() }

        // The first add queues a sync that hangs on the Cloud; everything after must still return.
        let first = try await state.addServer(urlText: "https://one.test", token: "wc_pat_1", name: "One")
        try await waitUntil { world.waiting == 1 }
        let second = try await state.addServer(urlText: "https://two.test", token: "wc_pat_2", name: "Two")
        #expect(world.waiting == 1)

        try state.rename(second.id, to: "Second")
        let agents = AgentsModel(server: second, api: state.api(for: second)) { list in
            await state.hooks.agentsChanged?(second, list)
        }
        await agents.load()
        #expect(!agents.isLoading)
        #expect(agents.agents.count == 2)
        #expect(world.waiting == 1)

        await state.remove(first.id)
        #expect(state.servers.map(\.id) == [second.id])
        #expect(world.waiting == 1)

        world.release()
        await model.agenda?.idle()
        #expect(world.entries.map(\.name) == ["Second"])
        #expect(world.entries.first?.agents.count == 2)
        #expect(model.agenda?.notice == nil)
    }

    @Test func latestAgentsWin() async throws {
        let model = await accountModel([home])
        let state = model.appState
        let sync = try #require(model.agenda)
        world.hold()
        defer { world.release() }

        sync.agentsChanged(state.servers[0], agents(1))
        try await waitUntil { world.waiting == 1 }
        sync.agentsChanged(state.servers[0], agents(2))
        sync.agentsChanged(state.servers[0], agents(3))
        world.release()
        await sync.idle()

        let puts = world.cloud.requests.filter { $0.method == "PUT" }
        let patches = world.cloud.requests.filter { $0.method == "PATCH" }
        // The launch sync added the server (POST + PUT); the three changes became one PATCH and one PUT, with
        // the last list.
        #expect(puts.count == 2)
        #expect(patches.count == 1)
        #expect(world.entries.first?.agents.map { $0["id"] } == ["ag0", "ag1", "ag2"])
    }

    @Test func deleteDuringSyncWritesNothingAfter() async throws {
        let model = await accountModel([home, lab])
        let sync = try #require(model.agenda)
        #expect(world.entries.count == 2)  // the launch sync wrote both servers
        world.hold()
        defer { world.release() }

        sync.pushAll()
        try await waitUntil { world.waiting == 1 }  // the first server's write hangs
        let generation = sync.generation
        let deleting = Task { await model.deleteAccount() }
        try await waitUntil { sync.generation != generation }
        world.release()
        await deleting.value
        await sync.idle()

        let requests = world.cloud.requests
        let deleteIndex = try #require(requests.firstIndex { $0.method == "DELETE" && $0.path == "/v1/account" })
        let writesAfter = requests[(deleteIndex + 1)...].filter { $0.method != "GET" && $0.path.hasPrefix("/v1/servers") }
        #expect(writesAfter.isEmpty)
        #expect(world.entries.isEmpty)
        #expect(model.state == .starting)
        #expect(model.appState.servers.allSatisfy { $0.cloudServerID == nil })
    }

    @Test func invalidatedJobKeepsNoAgendaID() async throws {
        let model = await accountModel([])
        let state = model.appState
        let sync = try #require(model.agenda)
        world.hold()
        defer { world.release() }

        let saved = try await state.addServer(urlText: "https://new.test", token: "wc_pat_n", name: "New")
        try await waitUntil { world.waiting == 1 }  // its POST hangs
        sync.invalidate()
        state.forgetAccount()
        world.release()
        await sync.idle()

        // The POST was already out, but its reply must not leave an agenda id behind.
        #expect(state.servers.first { $0.id == saved.id }?.cloudServerID == nil)
        #expect(sync.notice == nil)
    }

    @Test func changesDuringDeleteQueueNothing() async throws {
        let model = await accountModel([])
        let state = model.appState
        world.hold()
        defer { world.release() }

        let deleting = Task { await model.deleteAccount() }
        try await waitUntil { world.waiting == 1 }  // the DELETE hangs
        _ = try await state.addServer(urlText: "https://late.test", token: "wc_pat_l", name: "Late")
        world.release()
        await deleting.value
        await model.agenda?.idle()

        #expect(world.cloudRequests("POST", "/v1/servers").isEmpty)
        #expect(world.entries.isEmpty)
        #expect(model.state == .starting)
    }

    @Test func failedDeleteResumesTheSync() async throws {
        let model = await accountModel([])
        world.cloudFailure = 503
        await model.deleteAccount()
        #expect(model.state == .ready)
        world.cloudFailure = nil

        _ = try await model.appState.addServer(urlText: "https://after.test", token: "wc_pat_a", name: "After")
        await model.agenda?.idle()
        #expect(world.entries.map(\.name) == ["After"])
    }

    // MARK: - The Wristcall Cloud entry

    @Test func pendingSetupSkipsCloudEntry() async throws {
        let cloudID = world.seedCloudEntry()
        world.seed(name: "Other", url: "https://other.test")
        let (sync, state) = await sync([])

        let pending = await sync.pendingSetup()

        // Never a server to set up here: it is the Cloud's own, and the read also brings it in.
        #expect(pending.map(\.name) == ["Other"])
        #expect(state.cloudEntry?.id == cloudID)
        #expect(state.servers.isEmpty)
    }

    @Test func refreshCloudEntryReadsAgenda() async throws {
        let cloudID = world.seedCloudEntry(agents: [
            ["id": "ag_c", "slug": "c", "display_name": "C", "icon": "waveform", "call_type": "one-shot"],
        ])
        world.seed(name: "Other", url: "https://other.test")
        let (sync, state) = await sync([home])

        await sync.refreshCloudEntry()
        #expect(state.cloudEntry?.id == cloudID)
        #expect(state.cloudEntry?.agents.map(\.id) == ["ag_c"])
        #expect(state.allServers.map(\.id) == [ManagedServer.cloudEntryID, "home"])

        // The Cloud down: the entry the app has stays.
        world.cloudFailure = 503
        await sync.refreshCloudEntry()
        #expect(state.cloudEntry?.id == cloudID)
        #expect(sync.notice == nil)

        // Gone from the agenda (no plan any more): gone here too.
        world.cloudFailure = nil
        world.dropEntry(cloudID)
        await sync.refreshCloudEntry()
        #expect(state.cloudEntry == nil)
        #expect(state.allServers.map(\.id) == ["home"])
    }

    @Test func agendaNeverWritesCloudEntry() async throws {
        let cloudID = world.seedCloudEntry()
        let (sync, state) = await sync([home], agents: agents(2))
        await sync.refreshCloudEntry()
        let cloud = try #require(state.cloudServer)

        sync.pushAll()
        sync.serverAdded(cloud)
        sync.serverChanged(cloud)
        sync.agentsChanged(cloud, agents(3))
        sync.serverRemoved(cloud)
        await sync.idle()

        let writes = world.cloud.requests.filter { $0.method != "GET" && $0.path.hasPrefix("/v1/servers/\(cloudID)") }
        #expect(writes.isEmpty)
        #expect(world.entries.contains { $0.id == cloudID && $0.kind == "cloud" })
        #expect(world.entries.filter { $0.kind != "cloud" }.map(\.url) == ["https://home.test"])
        #expect(sync.notice == nil)
    }

    @Test func removeByURLSkipsCloudEntry() async throws {
        // A self-hosted server at the Cloud's address, with no known agenda id: only the self-hosted entry goes.
        let cloudID = world.seedCloudEntry()
        let twin = ManagedServer(id: "twin", name: "Twin", url: URL(string: "https://cloud.wristcall.test")!,
                                 token: "wc_pat_t")
        let (sync, state) = await sync([])

        sync.serverRemoved(twin)
        await sync.idle()

        #expect(world.entries.map(\.id) == [cloudID])
        #expect(world.cloudRequests("DELETE", "/v1/servers/\(cloudID)").isEmpty)
        #expect(state.cloudEntry?.id == cloudID)
    }

    private func waitUntil(_ condition: () -> Bool) async throws {
        for _ in 0..<200 where !condition() {
            try await Task.sleep(for: .milliseconds(10))
        }
        #expect(condition())
    }
}
