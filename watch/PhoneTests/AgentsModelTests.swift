import Foundation
import Testing
import WristcallKit
@testable import WristcallPhone

@MainActor
struct AgentsModelTests {
    private let server = ManagedServer(name: "Home", url: URL(string: "https://srv.test")!, token: "wc_pat_x")

    private func agents(_ count: Int) -> [AgentDetail] {
        (0..<count).map { .sample(slug: "a\($0)", displayName: "Agent \($0)", position: $0) }
    }

    @Test func loadSortsByPosition() async {
        let fake = FakeServerAPI()
        fake.agentList = [
            .sample(slug: "c", position: 2), .sample(slug: "a", position: 0), .sample(slug: "b", position: 1),
        ]
        fake.providerList = .sample
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        #expect(model.agents.map(\.slug) == ["a", "b", "c"])
        #expect(model.providers == .sample)
        #expect(model.error == nil)
    }

    @Test func deleteRemovesAndNotifies() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(3)
        var notified: [[String]] = []
        let model = AgentsModel(server: server, api: fake, onChange: { notified.append($0.map(\.slug)) })
        await model.load()
        notified = []
        await model.delete(model.agents[1])
        #expect(fake.deletedAgents == ["ag_a1"])
        #expect(model.agents.map(\.slug) == ["a0", "a2"])
        #expect(notified == [["a0", "a2"]])
    }

    @Test func deleteFailureKeepsAgentAndShowsMessage() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(2)
        fake.deleteAgentError = APIError.notFound
        var notified = 0
        let model = AgentsModel(server: server, api: fake, onChange: { _ in notified += 1 })
        await model.load()
        notified = 0
        await model.delete(model.agents[0])
        #expect(model.agents.count == 2)
        #expect(model.error == APIError.notFound.message)
        #expect(notified == 0)
    }

    @Test func moveSendsPosition() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(4)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        await model.move(from: IndexSet(integer: 3), to: 1)
        #expect(fake.updatedAgents.count == 1)
        #expect(fake.updatedAgents[0].ref == "ag_a3")
        #expect(fake.updatedAgents[0].fields == ["position": .int(1)])
        #expect(model.agents.map(\.slug) == ["a0", "a3", "a1", "a2"])
    }

    @Test func moveDownUsesAdjustedIndex() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(4)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        await model.move(from: IndexSet(integer: 0), to: 3)
        #expect(fake.updatedAgents[0].fields == ["position": .int(2)])
        #expect(model.agents.map(\.slug) == ["a1", "a2", "a0", "a3"])
    }

    @Test func moveToSamePlaceSendsNothing() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(3)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        await model.move(from: IndexSet(integer: 1), to: 2)
        await model.move(from: IndexSet(integer: 1), to: 1)
        #expect(fake.updatedAgents.isEmpty)
    }

    @Test func moveFailureReloadsAndKeepsMessage() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(3)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        fake.updateAgentError = APIError.rateLimited
        await model.move(from: IndexSet(integer: 2), to: 0)
        #expect(model.agents.map(\.slug) == ["a0", "a1", "a2"])
        #expect(model.error == APIError.rateLimited.message)
    }

    @Test func errorShowsMessage() async {
        let fake = FakeServerAPI()
        fake.agentsError = APIError.limit("agent limit reached (5)")
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        #expect(model.error == "agent limit reached (5)")
        #expect(model.agents.isEmpty)
    }

    @Test func unreachableServerShowsGenericMessage() async {
        let fake = FakeServerAPI()
        fake.agentsError = URLError(.notConnectedToInternet)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        #expect(model.error == "Can't reach the server.")
    }

    @Test func providersFailureIsReported() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(1)
        fake.providersError = APIError.forbidden("this needs a user API token")
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        #expect(model.agents.count == 1)
        #expect(model.providers == nil)
        #expect(model.error == "this needs a user API token")
    }

    @Test func saveCreatesThenReloadsAndNotifies() async {
        let fake = FakeServerAPI()
        var notified: [[String]] = []
        let model = AgentsModel(server: server, api: fake, onChange: { notified.append($0.map(\.slug)) })
        let message = await model.save(["slug": .string("inbox"), "display_name": .string("Inbox")], editing: nil)
        #expect(message == nil)
        #expect(fake.createdAgents.count == 1)
        #expect(model.agents.map(\.slug) == ["inbox"])
        #expect(notified.last == ["inbox"])
    }

    @Test func saveReturnsTheServerMessage() async {
        let fake = FakeServerAPI()
        fake.createAgentError = APIError.limit("agent limit reached (5)")
        let model = AgentsModel(server: server, api: fake)
        let message = await model.save(["slug": .string("inbox")], editing: nil)
        #expect(message == "agent limit reached (5)")
        #expect(model.agents.isEmpty)
    }

    @Test func saveEditUpdatesByID() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(2)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        let message = await model.save(["display_name": .string("Renamed")], editing: model.agents[1])
        #expect(message == nil)
        #expect(fake.updatedAgents[0].ref == "ag_a1")
        #expect(model.agents[1].displayName == "Renamed")
    }

    @Test func saveEditWithNothingChangedSendsNothing() async {
        let fake = FakeServerAPI()
        fake.agentList = agents(1)
        let model = AgentsModel(server: server, api: fake)
        await model.load()
        let message = await model.save([:], editing: model.agents[0])
        #expect(message == nil)
        #expect(fake.updatedAgents.isEmpty)
    }
}
