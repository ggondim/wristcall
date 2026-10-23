import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct AllAgentsModelTests {
    private let home = ManagedServer(id: "s1", name: "Home", url: URL(string: "https://home.test")!, token: "wc_pat_a")
    private let work = ManagedServer(id: "s2", name: "Work", url: URL(string: "https://work.test")!, token: "wc_pat_b")

    private func model(
        _ servers: [ManagedServer], fakes: [String: FakeServerAPI]
    ) -> AllAgentsModel {
        AllAgentsModel(servers: { servers }, api: { fakes[$0.id] ?? FakeServerAPI() })
    }

    /// The account's Wristcall Cloud entry, with one agent in the agenda.
    private let entry = CloudServer(
        id: "cs-9", name: "Wristcall Cloud", url: "https://cloud.wristcall.test", kind: "cloud", linked: true,
        agents: [CloudAgent(id: "ag_v", slug: "voice-notes", displayName: "Voice notes", icon: "note.text",
                            callType: "one-shot", color: .yellow)])

    /// `own` saved here (all answered by `ownAPI`), the Cloud entry (answered by `cloudAPI`) when `withEntry`.
    private func cloudState(
        own: [ManagedServer], ownAPI: FakeServerAPI = FakeServerAPI(), cloudAPI: FakeServerAPI = FakeServerAPI(),
        withEntry: Bool = true
    ) -> AppState {
        let state = AppState(store: InMemoryManagedServerStore(own), makeAPI: { _, _ in ownAPI })
        _ = state.loadStoreIfNeeded()
        if withEntry {
            state.setCloudEntry(entry)
            state.cloudAPI = cloudAPI
        }
        return state
    }

    @Test func oneSectionPerServerKeepsOtherServersOnError() async {
        let ok = FakeServerAPI()
        ok.agentList = [.sample(slug: "b", position: 1), .sample(slug: "a", position: 0)]
        let down = FakeServerAPI()
        down.agentsError = APIError.network(.cannotConnectToHost)
        let model = model([work, home], fakes: ["s1": ok, "s2": down])
        await model.load()
        #expect(model.didLoad)
        // The order of the servers, not of the answers.
        #expect(model.sections.map(\.server.id) == ["s2", "s1"])
        #expect(model.sections[0].agents.isEmpty)
        #expect(model.sections[0].error == APIError.network(.cannotConnectToHost).message)
        #expect(model.sections[1].agents.map(\.slug) == ["a", "b"])
        #expect(model.sections[1].error == nil)
    }

    @Test func failedReloadKeepsWhatWasOnScreen() async {
        let fake = FakeServerAPI()
        fake.agentList = [.sample(slug: "a")]
        let model = model([home], fakes: ["s1": fake])
        await model.load()
        fake.agentsError = APIError.unauthorized
        await model.reload(server: home)
        #expect(model.sections[0].agents.map(\.slug) == ["a"])
        #expect(model.sections[0].error == APIError.unauthorized.message)
        fake.agentsError = nil
        fake.agentList = [.sample(slug: "a"), .sample(slug: "c", position: 1)]
        await model.reload(server: home)
        #expect(model.sections[0].agents.map(\.slug) == ["a", "c"])
        #expect(model.sections[0].error == nil)
    }

    @Test func reloadTouchesOnlyItsServer() async {
        let a = FakeServerAPI()
        a.agentList = [.sample(slug: "a")]
        let b = FakeServerAPI()
        b.agentList = [.sample(slug: "b")]
        let model = model([home, work], fakes: ["s1": a, "s2": b])
        await model.load()
        a.agentList = []
        b.agentList = [.sample(slug: "b"), .sample(slug: "b2", position: 1)]
        await model.reload(server: work)
        #expect(model.sections.map { $0.agents.map(\.slug) } == [["a"], ["b", "b2"]])
        #expect(model.firstAgentName == "Notes")
    }

    @Test func removedServersLoseTheirSection() async {
        var servers = [home, work]
        let model = AllAgentsModel(servers: { servers }, api: { _ in FakeServerAPI() })
        await model.load()
        servers = [work]
        await model.load()
        #expect(model.sections.map(\.server.id) == ["s2"])
    }

    // MARK: - What a row says

    @Test func subtitleNamesTheHostOfTheAction() {
        let chat = AgentDetail.sample(action: .object([
            "type": .string("openai_chat"), "base_url": .string("https://api.openai.com/v1"),
        ]))
        #expect(AgentSummary.subtitle(chat) == "Conversation · api.openai.com")
        let hook = AgentDetail.sample(callType: "one-shot", action: .object([
            "type": .string("webhook"), "url": .string("https://hooks.example.org/notes"),
        ]))
        #expect(AgentSummary.subtitle(hook) == "One-shot · hooks.example.org")
        #expect(AgentSummary.subtitle(.sample()) == "Conversation · your server")
        #expect(AgentSummary.subtitle(.sample(callType: "monologue")) == "Monologue · your server")
        #expect(AgentSummary.subtitle(.sample(action: .object(["type": .string("openai_chat")]))) == "Conversation · your server")
    }

    @Test func footerNamesTheFirstAgentForSiri() {
        #expect(AgentSummary.footer(firstAgent: "Assistant")
            == "Tap an agent to edit it. Call agents from your watch, or ask Siri: \u{201C}Call Assistant with wristcall\u{201D}.")
        #expect(AgentSummary.footer(firstAgent: nil) == nil)
    }

    // MARK: - Wristcall Cloud entry

    @Test func cloudSectionComesFirst() async throws {
        let own = FakeServerAPI()
        own.agentList = [.sample(slug: "h")]
        let cloud = FakeServerAPI()
        cloud.agentList = [.sample(slug: "v")]
        let state = cloudState(own: [home, work], ownAPI: own, cloudAPI: cloud)
        let model = AllAgentsModel(state: state)

        await model.load()

        try #require(model.sections.map(\.server.id) == [ManagedServer.cloudEntryID, "s1", "s2"])
        #expect(model.sections[0].agents.map(\.slug) == ["v"])
        #expect(model.sections[1].agents.map(\.slug) == ["h"])
        #expect(AgentsHomeLayout.header(for: model.sections[0].server) == "WRISTCALL CLOUD \u{00B7} BETA")
        #expect(AgentsHomeLayout.header(for: model.sections[1].server) == "HOME")
        // Whatever order the servers come in, the Cloud entry leads.
        let cloudServer = try #require(state.cloudServer)
        let shuffled = self.model([home, cloudServer], fakes: ["s1": own, ManagedServer.cloudEntryID: cloud])
        await shuffled.load()
        #expect(shuffled.sections.map(\.server.id) == [ManagedServer.cloudEntryID, "s1"])
    }

    @Test func cloudSectionFallsBackToAgendaAgents() async throws {
        let own = FakeServerAPI()
        own.agentList = [.sample(slug: "h")]
        let cloud = FakeServerAPI()
        cloud.agentsError = APIError.noNodeAvailable(retryAfter: nil)
        let model = AllAgentsModel(state: cloudState(own: [home], ownAPI: own, cloudAPI: cloud))

        await model.load()

        try #require(model.sections.map(\.server.id) == [ManagedServer.cloudEntryID, "s1"])
        #expect(model.sections[0].agents.map(\.displayName) == ["Voice notes"])
        #expect(model.sections[0].agents.map(\.id) == ["ag_v"])
        #expect(model.sections[0].error == "Wristcall Cloud is temporarily unavailable. Try again in a minute.")
        // The own server is not affected.
        #expect(model.sections[1].agents.map(\.slug) == ["h"])
        #expect(model.sections[1].error == nil)
    }

    @Test func cloudReloadFailureShowsAgendaNotWhatWasOnScreen() async throws {
        let cloud = FakeServerAPI()
        cloud.agentList = [.sample(slug: "stale")]
        let state = cloudState(own: [home], cloudAPI: cloud)
        let model = AllAgentsModel(state: state)
        await model.load()
        try #require(model.sections[0].agents.map(\.slug) == ["stale"])

        cloud.agentsError = APIError.noNodeAvailable(retryAfter: nil)
        await model.reload(server: try #require(state.cloudServer))

        // The Cloud entry shows the agenda; an own server (see failedReloadKeepsWhatWasOnScreen) keeps its agents.
        #expect(model.sections[0].agents.map(\.id) == ["ag_v"])
        #expect(model.sections[0].error != nil)
    }

    @Test func cloudOnlyAccountIsNotEmpty() async throws {
        let cloud = FakeServerAPI()
        cloud.agentList = [.sample(slug: "v")]
        let state = cloudState(own: [], cloudAPI: cloud)
        let model = AllAgentsModel(state: state)
        await model.load()

        let layout = AgentsHomeLayout(servers: state.allServers, hasPlan: true)
        #expect(!layout.isEmpty)
        #expect(layout.canCreateAgent)
        #expect(layout.cloudNotice == .none)
        #expect(model.sections.map(\.server.id) == [ManagedServer.cloudEntryID])
        // Without an own server, "New agent" goes to the Cloud entry.
        let target = AgentsHomeLayout.newAgentServer(sections: model.sections, own: state.servers, cloud: state.cloudServer)
        #expect(target?.id == ManagedServer.cloudEntryID)
    }

    @Test func lapsedPlanStillShowsCloudSection() async {
        let state = cloudState(own: [])
        let model = AllAgentsModel(state: state)
        await model.load()

        // No plan (it lapsed) but the entry is still there: its section shows, no invitation.
        let layout = AgentsHomeLayout(servers: state.allServers, hasPlan: false)
        #expect(!layout.isEmpty)
        #expect(layout.cloudNotice == .none)
        #expect(model.sections.map(\.server.id) == [ManagedServer.cloudEntryID])
        let withOwn = AgentsHomeLayout(servers: cloudState(own: [home]).allServers, hasPlan: false)
        #expect(withOwn.cloudNotice == .none)
    }

    @Test func planWithoutEntrySaysSettingUp() {
        let own = cloudState(own: [home], withEntry: false)
        let none = cloudState(own: [], withEntry: false)

        #expect(AgentsHomeLayout(servers: own.allServers, hasPlan: true).cloudNotice == .settingUp)
        let empty = AgentsHomeLayout(servers: none.allServers, hasPlan: true)
        #expect(empty.isEmpty)
        #expect(!empty.canCreateAgent)
        #expect(empty.cloudNotice == .settingUp)
        #expect(AgentsHomeLayout.settingUpText == "Setting up Wristcall Cloud\u{2026}")
        // No plan and no entry: the invitation, only where it is today (without servers).
        #expect(AgentsHomeLayout(servers: none.allServers, hasPlan: false).cloudNotice == .tryCloud)
        #expect(AgentsHomeLayout(servers: own.allServers, hasPlan: false).cloudNotice == .none)
    }

    @Test func newAgentPrefersOwnServer() async throws {
        let state = cloudState(own: [home, work])
        let cloud = try #require(state.cloudServer)
        let ok = AllAgentsModel.Section(server: cloud, agents: [], error: nil)
        let homeDown = AllAgentsModel.Section(server: home, agents: [], error: "down")
        let workUp = AllAgentsModel.Section(server: work, agents: [], error: nil)

        // The first own server that answered, never the Cloud entry ahead of it.
        #expect(AgentsHomeLayout.newAgentServer(sections: [ok, homeDown, workUp], own: state.servers, cloud: cloud)?.id
            == "s2")
        #expect(AgentsHomeLayout.newAgentServer(sections: [ok, workUp, homeDown], own: state.servers, cloud: cloud)?.id
            == "s2")
        // None answered (or nothing loaded yet): still the first own server.
        #expect(AgentsHomeLayout.newAgentServer(sections: [ok, homeDown], own: [home], cloud: cloud)?.id == "s1")
        #expect(AgentsHomeLayout.newAgentServer(sections: [], own: [home, work], cloud: cloud)?.id == "s1")
        // No own server: the Cloud entry; nothing at all: no editor.
        #expect(AgentsHomeLayout.newAgentServer(sections: [ok], own: [], cloud: cloud)?.id == ManagedServer.cloudEntryID)
        #expect(AgentsHomeLayout.newAgentServer(sections: [], own: [], cloud: nil) == nil)
    }
}
