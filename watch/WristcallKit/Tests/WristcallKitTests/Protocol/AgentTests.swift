import Foundation
import Testing
import WristcallKit

struct AgentTests {
    private func agent(_ json: String) throws -> Agent {
        try JSONDecoder().decode(Agent.self, from: Data(json.utf8))
    }

    @Test(arguments: [
        ("conversation", CallType.conversation),
        ("one-shot", .oneShot),
        ("monologue", .monologue),
        ("telepathy", .unknown("telepathy")),
    ])
    func callTypeWireValues(wire: String, type: CallType) throws {
        #expect(try JSONDecoder().decode([CallType].self, from: Data("[\"\(wire)\"]".utf8)) == [type])
        #expect(type.wireValue == wire)
    }

    @Test func oneWayAndSupported() {
        #expect(!CallType.conversation.isOneWay)
        #expect(CallType.oneShot.isOneWay)
        #expect(CallType.monologue.isOneWay)
        #expect(!CallType.unknown("x").isOneWay)
        #expect(CallType.conversation.isSupported)
        #expect(CallType.oneShot.isSupported)
        #expect(CallType.monologue.isSupported)
        #expect(!CallType.unknown("x").isSupported)
    }

    @Test func decodesTheFullSummary() throws {
        let decoded = try agent("""
            {"id":"ag_3f9c0a1b2c3d","slug":"note","display_name":"Note","icon":"note.text",
             "call_type":"one-shot","turn_end":"manual","extra":true}
            """)
        #expect(decoded == Agent(
            id: "ag_3f9c0a1b2c3d", slug: "note", displayName: "Note",
            icon: "note.text", callType: .oneShot, turnEnd: .manual
        ))
        #expect(decoded.id == "ag_3f9c0a1b2c3d")
    }

    @Test func missingIconAndCallTypeUseTheDefaults() throws {
        let decoded = try agent(#"{"id":"ag_1","slug":"demo","display_name":"Demo"}"#)
        #expect(decoded.icon == "waveform")
        #expect(decoded.callType == .conversation)
        #expect(decoded.turnEnd == .auto)
    }

    @Test func unknownCallTypeIsKept() throws {
        let decoded = try agent(#"{"id":"ag_1","slug":"demo","display_name":"Demo","call_type":"hologram"}"#)
        #expect(decoded.callType == .unknown("hologram"))
        #expect(!decoded.callType.isSupported)
    }

    @Test func unknownTurnEndFallsBackToAuto() throws {
        let decoded = try agent(#"{"id":"ag_1","slug":"demo","display_name":"Demo","turn_end":"telepathic"}"#)
        #expect(decoded.turnEnd == .auto)
    }

    @Test func missingRequiredFieldThrows() {
        #expect(throws: DecodingError.self) {
            try agent(#"{"id":"ag_1","display_name":"Demo"}"#)
        }
    }

    @Test func initFromAProfileFollowsDecisionW6() {
        let converted = Agent(profile: Profile(name: "demo", displayName: "Demo"))
        #expect(converted == Agent(id: "demo", slug: "demo", displayName: "Demo", icon: "waveform", callType: .conversation, turnEnd: .auto))
    }

    @Test func initDefaults() {
        let made = Agent(id: "ag_1", slug: "s", displayName: "S")
        #expect(made.icon == "waveform")
        #expect(made.callType == .conversation)
        #expect(made.turnEnd == .auto)
    }

    @Test func userInfoDecodes() throws {
        let full = try JSONDecoder().decode(UserInfo.self, from: Data(#"{"id":"u_1","handle":"ana","display_name":"Ana"}"#.utf8))
        #expect(full == UserInfo(id: "u_1", handle: "ana", displayName: "Ana"))
        let bare = try JSONDecoder().decode(UserInfo.self, from: Data(#"{"id":"u_1","handle":"ana","display_name":null}"#.utf8))
        #expect(bare.displayName == nil)
        let absent = try JSONDecoder().decode(UserInfo.self, from: Data(#"{"id":"u_1","handle":"ana"}"#.utf8))
        #expect(absent.displayName == nil)
    }
}
