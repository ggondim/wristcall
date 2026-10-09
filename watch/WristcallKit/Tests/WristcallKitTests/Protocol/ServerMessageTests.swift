import Foundation
import Testing
import WristcallKit

struct ServerMessageTests {
    @Test func sessionReadyFromTheProtocolExample() throws {
        let text = """
            {"type":"session.ready","session_id":"9f2c...","profile":{"name":"default","display_name":"Agent"},
             "audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}
            """
        let expected = SessionReady(
            sessionID: "9f2c...",
            profile: Profile(name: "default", displayName: "Agent"),
            audioOut: AudioFormat(codec: "pcm16", sampleRate: 24_000, channels: 1)
        )
        #expect(try ServerMessage.decode(text) == .sessionReady(expected))
    }

    @Test(arguments: [
        (#"{"type":"turn.user_end","reason":"vad"}"#, ServerMessage.userTurnEnded(.vad)),
        (#"{"type":"turn.user_end","reason":"mute"}"#, .userTurnEnded(.mute)),
        (#"{"type":"turn.user_end","reason":"limit"}"#, .userTurnEnded(.limit)),
        (#"{"type":"turn.user_end","reason":"sneeze"}"#, .userTurnEnded(.unknown("sneeze"))),
        (#"{"type":"transcript","role":"user","text":"hello"}"#, .transcript(Transcript(role: .user, text: "hello"))),
        (#"{"type":"transcript","role":"assistant","text":"You said: hello"}"#, .transcript(Transcript(role: .assistant, text: "You said: hello"))),
        (#"{"type":"transcript","role":"narrator","text":"x"}"#, .transcript(Transcript(role: .unknown("narrator"), text: "x"))),
        (#"{"type":"turn.agent_start"}"#, .agentTurnStarted),
        (#"{"type":"turn.agent_end"}"#, .agentTurnEnded),
    ])
    func turnMessages(text: String, expected: ServerMessage) throws {
        #expect(try ServerMessage.decode(text) == expected)
    }

    @Test(arguments: [
        ("bad_message", ServerErrorCode.badMessage),
        ("not_started", .notStarted),
        ("unsupported_protocol", .unsupportedProtocol),
        ("unsupported_audio", .unsupportedAudio),
        ("unknown_profile", .unknownProfile),
        ("agent_unavailable", .agentUnavailable),
        ("stt_failed", .sttFailed),
        ("responder_failed", .responderFailed),
        ("tts_failed", .ttsFailed),
        ("internal", .internal),
        ("brand_new", .unknown("brand_new")),
    ])
    func errorCodes(wire: String, code: ServerErrorCode) throws {
        let text = #"{"type":"error","code":"\#(wire)","message":"m","fatal":false}"#
        #expect(try ServerMessage.decode(text) == .error(ServerError(code: code, message: "m", fatal: false)))
        #expect(code.wireValue == wire)
    }

    @Test func fatalErrorFromTheServer() throws {
        let text = #"{"type":"error","code":"unknown_profile","message":"profile 'x' does not exist","fatal":true}"#
        let expected = ServerError(code: .unknownProfile, message: "profile 'x' does not exist", fatal: true)
        #expect(try ServerMessage.decode(text) == .error(expected))
    }

    @Test func errorWithoutFatalIsNotFatal() throws {
        let text = #"{"type":"error","code":"internal","message":"boom"}"#
        #expect(try ServerMessage.decode(text) == .error(ServerError(code: .internal, message: "boom", fatal: false)))
    }

    @Test func unknownFieldsAreIgnored() throws {
        let ready = """
            {"type":"session.ready","session_id":"s1","extra":[1,2],
             "profile":{"name":"demo","display_name":"Demo","voice":"x"},
             "audio_out":{"codec":"pcm16","sample_rate":16000,"channels":1,"frame_ms":20}}
            """
        let expected = SessionReady(
            sessionID: "s1",
            profile: Profile(name: "demo", displayName: "Demo"),
            audioOut: AudioFormat(codec: "pcm16", sampleRate: 16_000, channels: 1)
        )
        #expect(try ServerMessage.decode(ready) == .sessionReady(expected))
        #expect(try ServerMessage.decode(#"{"type":"turn.agent_end","took_ms":812}"#) == .agentTurnEnded)
    }

    @Test func sessionReadyFromTheServer050Example() throws {
        let text = """
            {"type":"session.ready","session_id":"9f2c...","profile":{"name":"default","display_name":"Agent"},
             "agent":{"id":"ag_3f9c0a1b2c3d","slug":"default","display_name":"Agent","icon":"waveform",
                      "call_type":"conversation","turn_end":"auto"},
             "turn_end":"auto","audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}
            """
        let expected = SessionReady(
            sessionID: "9f2c...",
            profile: Profile(name: "default", displayName: "Agent"),
            audioOut: AudioFormat(codec: "pcm16", sampleRate: 24_000, channels: 1),
            agent: Agent(id: "ag_3f9c0a1b2c3d", slug: "default", displayName: "Agent"),
            turnEnd: .auto
        )
        #expect(try ServerMessage.decode(text) == .sessionReady(expected))
    }

    @Test func oneWaySessionReadyCarriesTheCallID() throws {
        let text = """
            {"type":"session.ready","session_id":"s1","call_id":"c_5d1f",
             "profile":{"name":"note","display_name":"Note"},
             "agent":{"id":"ag_1","slug":"note","display_name":"Note","icon":"note.text","call_type":"one-shot","turn_end":"manual"},
             "turn_end":"manual","audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}
            """
        guard case .sessionReady(let ready) = try ServerMessage.decode(text) else {
            Issue.record("not a session.ready")
            return
        }
        #expect(ready.callID == "c_5d1f")
        #expect(ready.turnEnd == .manual)
        #expect(ready.agent?.callType == .oneShot)
        #expect(ready.agent?.icon == "note.text")
    }

    @Test func sessionReadyWithoutTheNewFieldsHasNils() throws {
        let text = #"{"type":"session.ready","session_id":"s1","profile":{"name":"demo","display_name":"Demo"},"audio_out":{"codec":"pcm16","sample_rate":16000,"channels":1}}"#
        guard case .sessionReady(let ready) = try ServerMessage.decode(text) else {
            Issue.record("not a session.ready")
            return
        }
        #expect(ready.agent == nil)
        #expect(ready.callID == nil)
        #expect(ready.turnEnd == nil)
    }

    @Test func unknownTurnEndInSessionReadyIsNil() throws {
        let text = #"{"type":"session.ready","session_id":"s1","turn_end":"hybrid","profile":{"name":"demo","display_name":"Demo"},"audio_out":{"codec":"pcm16","sample_rate":16000,"channels":1}}"#
        guard case .sessionReady(let ready) = try ServerMessage.decode(text) else {
            Issue.record("not a session.ready")
            return
        }
        #expect(ready.turnEnd == nil)
    }

    @Test func callCapturedFromTheProtocolExample() throws {
        let text = #"{"type":"call.captured","call_id":"c_5d1f","reason":"limit"}"#
        #expect(try ServerMessage.decode(text) == .callCaptured(CallCaptured(callID: "c_5d1f", reason: "limit")))
    }

    @Test func callCapturedKeepsAReasonItDoesNotKnow() throws {
        let text = #"{"type":"call.captured","call_id":"c_1","reason":"something_new"}"#
        #expect(try ServerMessage.decode(text) == .callCaptured(CallCaptured(callID: "c_1", reason: "something_new")))
    }

    @Test func unknownTypesAreTolerated() throws {
        #expect(try ServerMessage.decode(#"{"type":"turn.something_new","foo":1}"#) == .unknown(type: "turn.something_new"))
    }

    @Test func decodesFromData() throws {
        #expect(try ServerMessage.decode(Data(#"{"type":"turn.agent_start"}"#.utf8)) == .agentTurnStarted)
    }

    @Test(arguments: [
        "not json",
        "[]",
        #"{"no_type":1}"#,
        #"{"type":7}"#,
        #"{"type":"transcript","role":"user"}"#,
        #"{"type":"session.ready","session_id":"s1"}"#,
        #"{"type":"turn.user_end"}"#,
        #"{"type":"call.captured","reason":"limit"}"#,
    ])
    func malformedMessagesThrow(text: String) {
        #expect(throws: ProtocolError.self) {
            try ServerMessage.decode(text)
        }
    }
}
