import Foundation
import Testing
import WristcallKit
@testable import Wristcall

struct AgentEntityTests {
    let catalog = AgentCatalog(defaults: UserDefaults(suiteName: "AgentEntityTests.\(UUID().uuidString)")!)
    let assistant = CatalogAgent(
        ref: AgentRef(serverID: "srv-1", agentID: "ag_1"), slug: "default", displayName: "Agent",
        icon: "waveform", callType: "conversation", serverHost: "agent.example.com")
    let house = CatalogAgent(
        ref: AgentRef(serverID: "srv-2", agentID: "ag_9"), slug: "house", displayName: "House",
        icon: "house.fill", callType: "conversation", serverHost: "home.example.com")

    @Test func entityShowsTheAgentAndItsServer() {
        let entity = AgentEntity(house)
        #expect(entity.id == "srv-2/ag_9")
        #expect(entity.displayName == "House")
        #expect(entity.icon == "house.fill")
        #expect(entity.serverHost == "home.example.com")
    }

    @Test func queryFindsTheAgentsOfTheCatalogByID() async throws {
        catalog.save([assistant, house])
        let query = AgentQuery(catalog: catalog)

        let found = try await query.entities(for: ["srv-2/ag_9", "srv-1/ag_1"])

        #expect(found.map(\.id) == ["srv-2/ag_9", "srv-1/ag_1"])
    }

    /// A complication configured for an agent that is gone resolves to nothing, not to another one.
    @Test func queryLeavesOutAgentsThatAreGone() async throws {
        catalog.save([assistant])
        let query = AgentQuery(catalog: catalog)

        let found = try await query.entities(for: ["srv-2/ag_9", "srv-1/ag_1", "not-a-ref"])

        #expect(found.map(\.id) == ["srv-1/ag_1"])
    }

    @Test func querySuggestsEveryAgentInTheCatalog() async throws {
        catalog.save([assistant, house])

        let suggested = try await AgentQuery(catalog: catalog).suggestedEntities()

        #expect(suggested.map(\.id) == ["srv-1/ag_1", "srv-2/ag_9"])
    }

    @Test func queryWithAnEmptyCatalogSuggestsNothing() async throws {
        #expect(try await AgentQuery(catalog: catalog).suggestedEntities().isEmpty)
    }
}
