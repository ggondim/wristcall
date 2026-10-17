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
}
