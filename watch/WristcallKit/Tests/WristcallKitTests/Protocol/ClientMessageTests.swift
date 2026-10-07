import Foundation
import Testing
import WristcallKit

struct ClientMessageTests {
    /// Parses JSON text into a Foundation object so key order does not matter.
    private func object(_ text: String) throws -> NSDictionary {
        try #require(try JSONSerialization.jsonObject(with: Data(text.utf8)) as? NSDictionary)
    }

    @Test func sessionStartMatchesTheProtocolExample() throws {
        let text = try ClientMessage.sessionStart(profile: "default").jsonText()
        let expected = """
            {"type":"session.start","protocol":1,"profile":"default",
             "audio_in":{"codec":"pcm16","sample_rate":16000,"channels":1}}
            """
        #expect(try object(text) == object(expected))
    }

    @Test func sessionStartIsCompactWithSortedKeys() throws {
        let text = try ClientMessage.sessionStart(profile: "demo").jsonText()
        #expect(text == #"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"profile":"demo","protocol":1,"type":"session.start"}"#)
    }

    @Test func sessionStartWithoutProfileOmitsTheKey() throws {
        let text = try ClientMessage.sessionStart(profile: nil).jsonText()
        #expect(text == #"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"protocol":1,"type":"session.start"}"#)
    }

    @Test func autoTurnEndIsOmittedSoTheBytesMatchOlderClients() throws {
        let explicit = try ClientMessage.sessionStart(profile: "demo", turnEnd: .auto).jsonText()
        #expect(explicit == #"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"profile":"demo","protocol":1,"type":"session.start"}"#)
        #expect(try ClientMessage.sessionStart(profile: "demo").jsonText() == explicit)
    }

    @Test func manualTurnEndIsSentOnTheWire() throws {
        let text = try ClientMessage.sessionStart(profile: "demo", turnEnd: .manual).jsonText()
        #expect(text == #"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"profile":"demo","protocol":1,"turn_end":"manual","type":"session.start"}"#)
    }

    @Test func manualTurnEndWithoutProfile() throws {
        let text = try ClientMessage.sessionStart(profile: nil, turnEnd: .manual).jsonText()
        #expect(text == #"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"protocol":1,"turn_end":"manual","type":"session.start"}"#)
    }

    @Test func turnEndWireValues() {
        #expect(TurnEnd.auto.rawValue == "auto")
        #expect(TurnEnd.manual.rawValue == "manual")
    }

    @Test(arguments: [
        (ClientMessage.mute(true), #"{"muted":true,"type":"mute"}"#),
        (ClientMessage.mute(false), #"{"muted":false,"type":"mute"}"#),
        (ClientMessage.sessionEnd, #"{"type":"session.end"}"#),
    ])
    func controlMessages(message: ClientMessage, expected: String) throws {
        #expect(try message.jsonText() == expected)
    }

    @Test func profileNamesAreEscaped() throws {
        let text = try ClientMessage.sessionStart(profile: #"a"b/ç"#).jsonText()
        #expect(try object(text)["profile"] as? String == #"a"b/ç"#)
    }
}
