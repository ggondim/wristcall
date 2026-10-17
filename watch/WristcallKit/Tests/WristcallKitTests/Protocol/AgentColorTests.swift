import Foundation
import Testing
@testable import WristcallKit

struct AgentColorTests {
    private static func agent(_ colorJSON: String) throws -> Agent {
        let json = #"{"id":"ag_1","slug":"a","display_name":"A"\#(colorJSON)}"#
        return try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    @Test func decodesKnownUnknownAndMissing() throws {
        #expect(try Self.agent(#","color":"teal""#).color == .teal)
        #expect(try Self.agent(#","color":"magenta""#).color == nil)
        #expect(try Self.agent(#","color":null"#).color == nil)
        #expect(try Self.agent("").color == nil)
        #expect(AgentColor.resolved(nil) == .mint)
        #expect(AgentColor.resolved(.pink) == .pink)
        #expect(AgentColor.allCases.map(\.rawValue)
            == ["red", "orange", "yellow", "green", "mint", "teal", "blue", "indigo", "purple", "pink"])
    }

    @Test func cloudAgentDecodesTolerantly() throws {
        let cloud = #"{"id":"a","slug":"s","display_name":"S","icon":"waveform","call_type":"conversation","color":"blue"}"#
        #expect(try JSONDecoder().decode(CloudAgent.self, from: Data(cloud.utf8)).color == .blue)
        let unknown = cloud.replacingOccurrences(of: "blue", with: "magenta")
        #expect(try JSONDecoder().decode(CloudAgent.self, from: Data(unknown.utf8)).color == nil)
        let without = cloud.replacingOccurrences(of: #","color":"blue""#, with: "")
        #expect(try JSONDecoder().decode(CloudAgent.self, from: Data(without.utf8)).color == nil)
    }

    @Test func cloudAgentOmitsNilColor() throws {
        let plain = CloudAgent(id: "a", slug: "s", displayName: "S", icon: "waveform", callType: "conversation")
        let plainJSON = try #require(String(data: JSONEncoder().encode(plain), encoding: .utf8))
        #expect(!plainJSON.contains("color"))
        let pink = CloudAgent(id: "a", slug: "s", displayName: "S", icon: "waveform", callType: "conversation", color: .pink)
        let pinkJSON = try #require(String(data: JSONEncoder().encode(pink), encoding: .utf8))
        #expect(pinkJSON.contains(#""color":"pink""#))
    }
}
