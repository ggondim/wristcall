import Foundation
import Testing
import WristcallKit
import WristcallKitTesting

/// `CallSession` driven by `FakeTransport` playing the server.
struct CallSessionTests {
    static let ready = #"{"type":"session.ready","session_id":"s1","profile":{"name":"demo","display_name":"Demo"},"audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}"#
    static let frame = Data(repeating: 1, count: ProtocolConstants.frameBytes)

    let transport = FakeTransport()

    func startText(_ profile: String?) throws -> FakeTransport.Sent {
        .text(try ClientMessage.sessionStart(profile: profile).jsonText())
    }

    func muteText(_ muted: Bool) throws -> FakeTransport.Sent {
        .text(try ClientMessage.mute(muted).jsonText())
    }

    func endText() throws -> FakeTransport.Sent {
        .text(try ClientMessage.sessionEnd.jsonText())
    }

    /// Starts the session and answers `session.ready`.
    func open(_ session: CallSession, profile: String? = "demo") async throws -> SessionReady {
        async let ready = session.start(profile: profile)
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverSends(Self.ready)
        return try await ready
    }

    // MARK: - Opening

    @Test func startSendsSessionStartAndReturnsReady() async throws {
        let session = CallSession(transport: transport)
        #expect(session.audioOut == nil)
        let ready = try await open(session)
        #expect(ready.sessionID == "s1")
        #expect(ready.profile == Profile(name: "demo", displayName: "Demo"))
        #expect(session.audioOut == AudioFormat(codec: "pcm16", sampleRate: 24_000, channels: 1))
        #expect(transport.sent == [try startText("demo")])
    }

    @Test func startWithoutProfileOmitsIt() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session, profile: nil)
        #expect(transport.sentTexts == [#"{"audio_in":{"channels":1,"codec":"pcm16","sample_rate":16000},"protocol":1,"type":"session.start"}"#])
    }

    @Test func startTwiceIsRefused() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        await #expect(throws: CallSessionError.alreadyStarted) {
            try await session.start(profile: nil)
        }
    }

    @Test func readyTimeoutEndsWithConnectionLost() async throws {
        let session = CallSession(transport: transport, readyTimeout: .milliseconds(100))
        await #expect(throws: CallSessionError.timedOut) {
            try await session.start(profile: nil)
        }
        #expect(await collect(session.events) == [.ended(.connectionLost)])
        #expect(transport.sent == [try startText(nil), .close(1000)])
    }

    @Test func connectFailureEndsWithConnectionLost() async throws {
        let transport = FakeTransport(connectError: .connectionFailed("refused"))
        let session = CallSession(transport: transport)
        await #expect(throws: CallSessionError.ended(.connectionLost)) {
            try await session.start(profile: nil)
        }
        #expect(await collect(session.events) == [.ended(.connectionLost)])
        #expect(transport.sent.isEmpty)
    }

    @Test func unauthorizedBeforeReady() async throws {
        let session = CallSession(transport: transport)
        let opening = Task { try await session.start(profile: nil) }
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverCloses(code: 4401)
        await #expect(throws: CallSessionError.ended(.unauthorized)) {
            try await opening.value
        }
        #expect(await collect(session.events) == [.ended(.unauthorized)])
    }

    @Test func fatalOpeningErrorEndsWithItsCode() async throws {
        let session = CallSession(transport: transport)
        let opening = Task { try await session.start(profile: "nope") }
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverSends(#"{"type":"error","code":"unknown_profile","message":"unknown profile: nope","fatal":true}"#)
        transport.serverCloses(code: 4400)
        await #expect(throws: CallSessionError.ended(.serverFatal(.unknownProfile))) {
            try await opening.value
        }
        #expect(await collect(session.events) == [
            .error(code: .unknownProfile, message: "unknown profile: nope", fatal: true),
            .ended(.serverFatal(.unknownProfile)),
        ])
    }

    @Test func endWhileWaitingForReady() async throws {
        let session = CallSession(transport: transport)
        let opening = Task { try await session.start(profile: nil) }
        try await transport.waitUntilSent { $0.count == 1 }
        await session.end()
        await #expect(throws: CallSessionError.ended(.normal)) {
            try await opening.value
        }
        #expect(await collect(session.events) == [.ended(.normal)])
        #expect(transport.sent == [try startText(nil), try endText(), .close(1000)])
    }

    @Test func endBeforeStart() async throws {
        let session = CallSession(transport: transport)
        await session.end()
        await #expect(throws: CallSessionError.ended(.normal)) {
            try await session.start(profile: nil)
        }
        #expect(await collect(session.events) == [.ended(.normal)])
        #expect(transport.sent.isEmpty)
        #expect(transport.connectCount == 0)
    }

    // MARK: - Audio and mute

    @Test func audioIsSentOnlyWhenReadyAndUnmuted() async throws {
        let session = CallSession(transport: transport)
        session.sendAudio(Data([9, 9]))  // before start: dropped
        _ = try await open(session)
        session.sendAudio(Self.frame)
        session.setMuted(true)
        session.sendAudio(Data([7, 7]))  // muted: dropped
        session.setMuted(false)
        session.sendAudio(Self.frame)
        try await transport.waitUntilSent { $0.count == 5 }
        #expect(transport.sent == [
            try startText("demo"), .binary(Self.frame), try muteText(true), try muteText(false), .binary(Self.frame),
        ])
    }

    @Test func muteBeforeReadyIsSentRightAfterReady() async throws {
        let session = CallSession(transport: transport)
        session.setMuted(true)
        #expect(session.isMuted)
        async let ready = session.start(profile: "demo")
        try await transport.waitUntilSent { $0.count == 1 }
        session.setMuted(true)  // still before ready: nothing goes out
        #expect(transport.sent == [try startText("demo")])
        transport.serverSends(Self.ready)
        _ = try await ready
        // Sent by the session itself, before any new setMuted call.
        try await transport.waitUntilSent { $0.count == 2 }
        #expect(transport.sent == [try startText("demo"), try muteText(true)])
        session.sendAudio(Self.frame)  // muted: dropped
        session.setMuted(true)  // repeated: nothing
        session.setMuted(false)
        try await transport.waitUntilSent { $0.count == 3 }
        #expect(transport.sent == [try startText("demo"), try muteText(true), try muteText(false)])
    }

    @Test func muteToggledBackBeforeReadySendsNothing() async throws {
        let session = CallSession(transport: transport)
        session.setMuted(true)
        session.setMuted(false)
        _ = try await open(session)
        session.sendAudio(Self.frame)
        try await transport.waitUntilSent { $0.count == 2 }
        #expect(transport.sent == [try startText("demo"), .binary(Self.frame)])
    }

    @Test func muteAndAudioAfterEndAreIgnored() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        await session.end()
        session.setMuted(true)
        session.sendAudio(Self.frame)
        #expect(!session.isMuted)
        #expect(transport.sent == [try startText("demo"), try endText(), .close(1000)])
    }

    // MARK: - Server events

    @Test func serverMessagesBecomeCallEvents() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        let agentAudio = Data(repeating: 2, count: 960)
        transport.serverSends(#"{"type":"turn.user_end","reason":"vad"}"#)
        transport.serverSends(#"{"type":"transcript","role":"user","text":"hello"}"#)
        transport.serverSends(#"{"type":"transcript","role":"assistant","text":"You said: hello"}"#)
        transport.serverSends(#"{"type":"turn.agent_start"}"#)
        transport.serverSends(binary: agentAudio)
        transport.serverSends(#"{"type":"something.new","x":1}"#)  // unknown type: ignored
        transport.serverSends("not json")  // malformed: ignored
        transport.serverSends(#"{"type":"turn.agent_end"}"#)
        transport.serverSends(#"{"type":"error","code":"tts_failed","message":"voice failed","fatal":false}"#)
        transport.serverCloses(code: 1000)
        #expect(await collect(session.events) == [
            .userTurnEnded(.vad),
            .transcript(role: .user, text: "hello"),
            .transcript(role: .assistant, text: "You said: hello"),
            .agentTurnStarted,
            .agentAudio(agentAudio),
            .agentTurnEnded,
            .error(code: .ttsFailed, message: "voice failed", fatal: false),
            .ended(.normal),
        ])
    }

    @Test func audioBeforeReadyIsNotAnEvent() async throws {
        let session = CallSession(transport: transport)
        async let ready = session.start(profile: nil)
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverSends(binary: Data([1, 2]))
        transport.serverSends(Self.ready)
        _ = try await ready
        transport.serverCloses(code: nil)
        #expect(await collect(session.events) == [.ended(.connectionLost)])
    }

    // MARK: - End

    @Test func endSendsSessionEndThenClosesWith1000() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        session.sendAudio(Self.frame)
        await session.end()
        await session.end()  // second call: nothing
        #expect(transport.sent == [try startText("demo"), .binary(Self.frame), try endText(), .close(1000)])
        #expect(await collect(session.events) == [.ended(.normal)])
    }

    @Test func endInTheMiddleOfAnAgentTurn() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        transport.serverSends(#"{"type":"turn.agent_start"}"#)
        transport.serverSends(binary: Data([1, 2]))
        var events = session.events.makeAsyncIterator()
        #expect(await events.next() == .agentTurnStarted)
        #expect(await events.next() == .agentAudio(Data([1, 2])))
        await session.end()
        transport.serverSends(binary: Data([3, 4]))  // after the end: not an event
        #expect(await events.next() == .ended(.normal))
        #expect(await events.next() == nil)
        #expect(transport.sent.suffix(2) == [try endText(), .close(1000)])
    }

    /// `FakeTransport` drops server frames once it is closed, so the test above cannot tell
    /// whether the session itself filters. `ScriptedTransport` keeps delivering, answers
    /// `session.end` with more of the agent's turn and a server close, and only lets `close`
    /// return once the session handled all of it, i.e. before `.ended` is emitted.
    @Test func eventsArrivingWhileEndingAreDropped() async throws {
        let transport = try ScriptedTransport(
            afterStart: [.text(#"{"type":"turn.agent_start"}"#), .binary(Data([1, 2]))],
            afterEnd: [
                .binary(Data([3, 4])),
                .text(#"{"type":"transcript","role":"assistant","text":"late"}"#),
                .text(#"{"type":"turn.agent_end"}"#),
                .text(#"{"type":"error","code":"internal","message":"late","fatal":true}"#),
                .closed(code: 1011),
            ]
        )
        defer { transport.finish() }
        let session = CallSession(transport: transport)
        _ = try await session.start(profile: "demo")
        var events = session.events.makeAsyncIterator()
        #expect(await events.next() == .agentTurnStarted)
        #expect(await events.next() == .agentAudio(Data([1, 2])))
        await session.end()
        #expect(await events.next() == .ended(.normal))
        #expect(await events.next() == nil)
        #expect(transport.sentTexts.last == (try ClientMessage.sessionEnd.jsonText()))
        #expect(transport.closeCodes == [1000])
    }

    /// Review Focus 1: hanging up never waits on the network longer than `endFlushTimeout`.
    @Test func endIsBoundedEvenIfASendHangs() async throws {
        let transport = try ScriptedTransport(hangingBinarySends: true, hangLimit: .seconds(6))
        defer { transport.finish() }
        let session = CallSession(transport: transport)
        _ = try await session.start(profile: "demo")
        session.sendAudio(Self.frame)  // its send never completes on its own
        let elapsed = await ContinuousClock().measure {
            await session.end()
        }
        #expect(elapsed >= CallSession.endFlushTimeout - .milliseconds(50))
        #expect(elapsed < CallSession.endFlushTimeout + .seconds(1))
        #expect(transport.closeCodes == [1000])
        #expect(await collect(session.events) == [.ended(.normal)])
    }

    @Test(arguments: [
        (UInt16?.none, CallEndReason.connectionLost),
        (UInt16?.some(1011), CallEndReason.connectionLost),
        (UInt16?.some(1001), CallEndReason.connectionLost),
        (UInt16?.some(4401), CallEndReason.unauthorized),
        (UInt16?.some(4400), CallEndReason.serverFatal(nil)),
        (UInt16?.some(1000), CallEndReason.normal),
    ])
    func serverCloseMidCall(code: UInt16?, expected: CallEndReason) async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        transport.serverCloses(code: code)
        #expect(await collect(session.events) == [.ended(expected)])
        // After the end, hanging up sends nothing more.
        await session.end()
        #expect(transport.sent == [try startText("demo")])
    }

    @Test func fatalErrorMidCall() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        transport.serverSends(#"{"type":"error","code":"internal","message":"boom","fatal":true}"#)
        transport.serverCloses(code: 4400)
        #expect(await collect(session.events) == [
            .error(code: .internal, message: "boom", fatal: true),
            .ended(.serverFatal(.internal)),
        ])
    }

    @Test func unauthorizedWinsOverAFatalError() async throws {
        let session = CallSession(transport: transport)
        _ = try await open(session)
        transport.serverSends(#"{"type":"error","code":"internal","message":"boom","fatal":true}"#)
        transport.serverCloses(code: 4401)
        let events = await collect(session.events)
        #expect(events.last == .ended(.unauthorized))
    }
}

/// All events until the stream finishes (or 2 s pass).
func collect(_ events: AsyncStream<CallEvent>, timeout: Duration = .seconds(2)) async -> [CallEvent] {
    await withTaskGroup(of: [CallEvent]?.self) { group in
        group.addTask {
            var all: [CallEvent] = []
            for await event in events {
                all.append(event)
            }
            return all
        }
        group.addTask {
            try? await Task.sleep(for: timeout)
            return nil
        }
        let first = await group.next() ?? nil
        group.cancelAll()
        return first ?? [.error(code: .unknown("test"), message: "event stream did not finish in \(timeout)", fatal: true)]
    }
}
