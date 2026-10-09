import Foundation
import Testing
import WristcallKit

struct AgentRefTests {
    @Test func textFormIsServerSlashAgent() {
        let ref = AgentRef(serverID: "srv-1", agentID: "ag_one")
        #expect(ref.description == "srv-1/ag_one")
        #expect(AgentRef("srv-1/ag_one") == ref)
        #expect(AgentRef(ref.description) == ref)
    }

    @Test func malformedTextIsRefused() {
        for text in ["", "/", "srv-1", "srv-1/", "/ag_one", "a/b/c", "srv-1//ag_one", "//"] {
            #expect(AgentRef(text) == nil, "\\(text)")
        }
    }

    @Test func roundTripsThroughJSON() throws {
        let ref = AgentRef(serverID: "srv-1", agentID: "ag_one")
        let data = try JSONEncoder().encode(ref)
        #expect(try JSONDecoder().decode(AgentRef.self, from: data) == ref)
    }

    @Test func worksAsALosslessStringConvertible() {
        #expect(String(describing: AgentRef(serverID: "s", agentID: "a")) == "s/a")
        #expect((AgentRef("s/a") as AgentRef?)?.agentID == "a")
    }
}
