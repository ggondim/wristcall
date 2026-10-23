import Foundation
import Testing
import WristcallKit
@testable import Wristcall

/// The Wristcall Cloud's agenda entry as a virtual server (id `cloud`): first in the grid, never in the
/// Keychain, never paired, published to the iPhone or registered for push.
@MainActor
struct CloudEntryTests {
    let store = InMemoryServerStore()
    let cloudEntries = InMemoryCloudEntryStore()
    let pairing = StubPairingService()
    let defaults = UserDefaults(suiteName: "CloudEntryTests.\(UUID().uuidString)")!

    let paired = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token", id: "srv-1")
    let info = DeviceInfo(
        deviceId: "dev-1", deviceName: "Apple Watch", user: UserInfo(id: "usr_1", handle: "gustavo"),
        agents: [Agent(id: "ag_1", slug: "default", displayName: "Agent")])
    let entry = CloudServer(
        id: "cs-cloud", name: "Wristcall Cloud", url: "https://cloud.example.com", kind: "cloud", linked: true,
        agents: [CloudAgent(id: "ag_c1", slug: "voice-notes", displayName: "Voice notes", icon: "mic",
                            callType: "conversation")])

    func makeModel() -> AppModel {
        AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in },
                 resultPoller: FakeClock().poller, cloudEntries: cloudEntries)
    }

    /// `paired` stored and answered, then the cloud entry applied.
    func makePairedModelWithEntry() async throws -> AppModel {
        try store.save([paired])
        pairing.setMeResults([.success(info)], for: paired.serverURL)
        let model = makeModel()
        await model.launch()
        try #require(model.phase == .home)
        model.applyCloudEntry(entry)
        return model
    }

    @Test func cloudEntryAgentsAppearFirstInGrid() async throws {
        var catalogs: [[CatalogAgent]] = []
        let model = try await makePairedModelWithEntry()
        model.onAgentsChanged = { catalogs.append($0) }
        model.applyCloudEntry(nil)
        model.applyCloudEntry(entry)

        #expect(model.agents.first?.agent.displayName == "Voice notes")
        #expect(model.agents.first?.serverHost == "Wristcall Cloud")
        #expect(model.agents.map(\.agent.id) == ["ag_c1", "ag_1"])
        #expect(model.servers.first?.id == "cloud")
        #expect(model.servers.first?.host == "Wristcall Cloud")
        #expect(model.catalog.first?.ref == AgentRef(serverID: "cloud", agentID: "ag_c1"))
        #expect(model.catalog.first?.serverHost == "Wristcall Cloud")
        #expect(catalogs.last == model.catalog)
        #expect(cloudEntries.load() == entry)
    }

    @Test func cloudEntryNeverSavedToKeychain() async throws {
        let model = makeModel()
        await model.launch()
        model.applyCloudEntry(entry)
        let second = URL(string: "https://home.example.com")!
        pairing.setMeResults([.success(info)], for: second)

        try await model.addPaired(PairedDevice(deviceId: "dev-9", token: "home-token"), server: second)

        let stored = try store.load()
        #expect(stored.map(\.serverURL) == [second])
        #expect(!stored.contains { $0.isCloudEntry })
        #expect(model.servers.map(\.id).first == Credentials.cloudEntryID)
        #expect(model.servers.count == 2)
    }

    @Test func cloudOnlyAccountStaysHome() async throws {
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unpaired)

        model.applyCloudEntry(entry)

        #expect(model.phase == .home)
        #expect(model.hasServers)
        #expect(model.canCall)
        #expect(try store.load().isEmpty)
        // Removing it does nothing: the entry is the account's, not the watch's.
        await model.removeServer(id: Credentials.cloudEntryID)
        #expect(model.servers.map(\.id) == [Credentials.cloudEntryID])
        #expect(pairing.calls.isEmpty)
    }

    @Test func cloudEntrySurvivesRelaunch() async throws {
        cloudEntries.save(entry)
        let model = makeModel()

        await model.launch()

        #expect(model.phase == .home)
        #expect(model.agents.map(\.agent.displayName) == ["Voice notes"])
        #expect(model.servers.first?.isReady == true)
        // No `/v1/me` (nor anything else) goes out for it.
        #expect(pairing.calls.isEmpty)

        // With a paired server too, only that one is asked.
        try store.save([paired])
        pairing.setMeResults([.success(info), .success(info)], for: paired.serverURL)
        let again = makeModel()
        await again.launch()
        await again.reloadAgents()
        #expect(again.servers.map(\.id) == [Credentials.cloudEntryID, "srv-1"])
        #expect(pairing.calls == ["me https://agent.example.com", "me https://agent.example.com"])
    }

    @Test func cloudEntryIsNeverPublishedOrPushed() async throws {
        let handler = RecordingPushHandler(pairing: pairing)
        var published: [[Credentials]] = []
        let model = makeModel()
        model.pushHandler = handler
        model.onServersChanged = { published.append($0) }
        await model.launch()

        model.applyCloudEntry(entry)
        let second = URL(string: "https://home.example.com")!
        pairing.setMeResults([.success(info)], for: second)
        try await model.addPaired(PairedDevice(deviceId: "dev-9", token: "home-token"), server: second)
        let secondID = try #require(model.servers.last?.id)
        model.applyCloudEntry(nil)

        #expect(published.allSatisfy { list in !list.contains { $0.isCloudEntry } })
        #expect(published.last?.map(\.id) == [secondID])
        #expect(model.pairedServers.map(\.id) == [secondID])
        #expect(handler.events == ["added \(secondID)"])
    }

    @Test func defaultCallStaysOnPairedServer() async throws {
        let handler = StubCallHandler()
        let model = try await makePairedModelWithEntry()
        model.callHandler = handler
        try #require(model.agents.first?.serverID == Credentials.cloudEntryID)

        model.startCall(agent: nil)
        #expect(handler.started.map(\.credentials) == [paired])
        #expect(handler.started.first?.target.agent.id == "ag_1")
        model.callDidEnd(.normal)

        // Without a paired server, the cloud entry is the default.
        let cloudOnly = AppModel(pairing: pairing, store: InMemoryServerStore(), defaults: defaults, sleep: { _ in },
                                 resultPoller: FakeClock().poller, cloudEntries: InMemoryCloudEntryStore(entry))
        cloudOnly.callHandler = handler
        await cloudOnly.launch()
        cloudOnly.startCall(agent: nil)
        #expect(handler.started.last?.credentials.isCloudEntry == true)
        #expect(handler.started.last?.target.agent.id == "ag_c1")
    }

    /// The test bundle is hosted by the watch app: `Bundle.main` is `Wristcall.app`.
    @Test func bundleSendsWatchClientHeader() {
        #expect(ClientIdentity.fromBundle(.main, platform: .watchos)?.header == "watchos/0.6.0")
    }
}
