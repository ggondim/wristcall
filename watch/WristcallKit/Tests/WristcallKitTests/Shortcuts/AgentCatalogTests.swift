import Foundation
import Testing
import WristcallKit

struct AgentCatalogTests {
    let defaults = UserDefaults(suiteName: "AgentCatalogTests.\(UUID().uuidString)")!
    var catalog: AgentCatalog { AgentCatalog(defaults: defaults) }

    let agents = [
        CatalogAgent(
            ref: AgentRef(serverID: "srv-1", agentID: "ag_one"), slug: "default", displayName: "Agent",
            icon: "waveform", callType: "conversation", serverHost: "agent.example.com"),
        CatalogAgent(
            ref: AgentRef(serverID: "srv-2", agentID: "ag_two"), slug: "notes", displayName: "Notes",
            icon: "note.text", callType: "one_way", serverHost: "home.example.com"),
    ]

    @Test func emptyAtFirst() {
        #expect(catalog.load().isEmpty)
    }

    @Test func savesAndLoadsInOrder() {
        catalog.save(agents)
        #expect(catalog.load() == agents)
        #expect(catalog.load().map(\.id) == ["srv-1/ag_one", "srv-2/ag_two"])
    }

    @Test func saveReplacesTheWholeCatalog() {
        catalog.save(agents)
        catalog.save([agents[1]])
        #expect(catalog.load() == [agents[1]])
        catalog.save([])
        #expect(catalog.load().isEmpty)
    }

    @Test func invalidDataIsAnEmptyCatalog() {
        defaults.set(Data("not json".utf8), forKey: AgentCatalog.defaultsKey)
        #expect(catalog.load().isEmpty)
        defaults.set("a string", forKey: AgentCatalog.defaultsKey)
        #expect(catalog.load().isEmpty)
    }

    @Test func theCatalogIsVisibleThroughAnotherInstanceOnTheSameDefaults() {
        catalog.save(agents)
        #expect(AgentCatalog(defaults: defaults).load() == agents)
    }

    @Test func storesUnderTheVersionedKey() {
        #expect(AgentCatalog.defaultsKey == "agentCatalog.v1")
        catalog.save(agents)
        #expect(defaults.data(forKey: "agentCatalog.v1") != nil)
    }

    @Test func sharedWithoutTheGroupInTheInfoPlistStillWorks() {
        #expect(AgentCatalog.appGroupInfoKey == "WristcallAppGroup")
        // The test bundle has no such key: the catalog falls back to the standard defaults.
        let shared = AgentCatalog.shared(bundle: Bundle(for: BundleToken.self))
        _ = shared.load()
    }

    private final class BundleToken {}
}
