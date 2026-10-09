import Foundation
import Synchronization
import Testing
import WristcallKit
import WristcallKitTesting
@testable import Wristcall

/// Stands in for `AudioIO`: records what the coordinator asks and lets the test play the microphone.
@MainActor
final class FakeCallAudio: CallAudio {
    var startError: (any Error)?
    private(set) var prepared = 0
    private(set) var playbackRates: [Int] = []
    private(set) var mutes: [Bool] = []
    private(set) var played: [Data] = []
    private(set) var turnEnds = 0
    private(set) var stops = 0
    private(set) var isRunning = false
    private var onFrame: (@Sendable (Data) -> Void)?

    func prepare() throws { prepared += 1 }

    func start(playbackSampleRate: Int, onFrame: @escaping @Sendable (Data) -> Void) throws {
        playbackRates.append(playbackSampleRate)
        if let startError { throw startError }
        self.onFrame = onFrame
        isRunning = true
    }

    func setMuted(_ muted: Bool) { mutes.append(muted) }
    func play(_ data: Data) { played.append(data) }
    func agentTurnEnded() { turnEnds += 1 }

    func stop() {
        stops += 1
        isRunning = false
        onFrame = nil
    }

    /// The microphone produced a frame (only while started, like the real tap).
    func microphone(_ frame: Data) {
        onFrame?(frame)
    }
}

@MainActor
struct CallCoordinatorTests {
    nonisolated static let ready = #"{"type":"session.ready","session_id":"s1","profile":{"name":"default","display_name":"Agent"},"audio_out":{"codec":"pcm16","sample_rate":24000,"channels":1}}"#
    nonisolated static let frame = Data(repeating: 7, count: ProtocolConstants.frameBytes)

    let store = InMemoryCredentialStore()
    let pairing = StubPairingService()
    let defaults = UserDefaults(suiteName: "CallCoordinatorTests.\(UUID().uuidString)")!
    let credentials = Credentials(serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token")
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", profiles: [Profile(name: "default", displayName: "Agent")])
    let callKit = FakeCallControl()
    let audio = FakeCallAudio()
    let transport = FakeTransport()
    let transportsMade = Recorder<Credentials>()

    /// `transport`: what the factory returns; `self.transport` when `nil`.
    func makeCoordinator(
        activationTimeout: Duration = .seconds(10),
        transport: FakeTransport? = nil
    ) async throws -> (CallCoordinator, AppModel) {
        try store.save(credentials)
        pairing.meResults = [.success(info)]
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in })
        await model.launch()
        try #require(model.phase == .ready(info))
        let transport = transport ?? self.transport
        let made = transportsMade
        let coordinator = CallCoordinator(callControl: callKit, audio: audio, activationTimeout: activationTimeout) { credentials in
            made.append(credentials)
            return transport
        }
        coordinator.model = model
        model.callHandler = coordinator
        return (coordinator, model)
    }

    /// Call → CallKit start → audio activated → session.ready.
    func connectedCall() async throws -> (CallCoordinator, AppModel) {
        let (coordinator, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }
        callKit.activateAudio()
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverSends(Self.ready)
        await waitUntil { audio.isRunning }
        try #require(audio.isRunning)
        return (coordinator, model)
    }

    /// One `reportEnded`, for the call CallKit was asked to start (no phantom call left behind).
    func endReportedForTheStartedCall(_ cause: CallEndCause) throws -> [FakeCallControl.End] {
        let start = try #require(callKit.starts.first)
        #expect(callKit.starts.count == 1)
        return [FakeCallControl.End(id: start.id, cause: cause)]
    }

    // MARK: - Opening

    @Test func callStartsCallKitWithTheProfileName() async throws {
        let (coordinator, model) = try await makeCoordinator()

        model.startCall()
        await waitUntil { callKit.starts.count == 1 }

        #expect(callKit.starts.first?.displayName == "Agent")
        #expect(callKit.starts.first?.id == coordinator.currentCallID)
        #expect(audio.prepared == 1)
        #expect(model.phase == .inCall(Profile(name: "default", displayName: "Agent")))
        #expect(model.callActivity == .connecting)
    }

    @Test func socketOpensOnlyAfterCallKitActivatesAudio() async throws {
        let (_, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }
        try await Task.sleep(for: .milliseconds(100))
        #expect(transportsMade.all.isEmpty)
        #expect(transport.connectCount == 0)

        callKit.activateAudio()

        try await transport.waitUntilSent { $0.count == 1 }
        #expect(transportsMade.all == [credentials])
        let start = try #require(transport.sentTexts.first)
        #expect(start.contains(#""type":"session.start""#))
        #expect(start.contains(#""profile":"default""#))
    }

    @Test func autoCallSendsTurnEndAutoInSessionStart() async throws {
        let (_, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }

        callKit.activateAudio()

        try await transport.waitUntilSent { $0.count == 1 }
        let start = try #require(transport.sentTexts.first)
        #expect(start.contains(#""type":"session.start""#))
        #expect(start.contains(#""turn_end":"auto""#))
    }

    @Test func manualCallSendsTurnEndManualInSessionStart() async throws {
        let (_, model) = try await makeCoordinator()
        model.startCall(turnEnd: .manual)
        await waitUntil { callKit.starts.count == 1 }

        callKit.activateAudio()

        try await transport.waitUntilSent { $0.count == 1 }
        let start = try #require(transport.sentTexts.first)
        #expect(start.contains(#""type":"session.start""#))
        #expect(start.contains(#""turn_end":"manual""#))
    }

    @Test func readyConnectsTheCallAndStartsTheAudioAtTheAgentRate() async throws {
        let (coordinator, model) = try await connectedCall()

        #expect(callKit.connected == [coordinator.currentCallID])
        #expect(audio.playbackRates == [24_000])
        #expect(model.callActivity == .listening)
    }

    @Test func microphoneFramesGoToTheServer() async throws {
        let (coordinator, _) = try await connectedCall()
        defer { withExtendedLifetime(coordinator) {} }

        audio.microphone(Self.frame)
        audio.microphone(Self.frame)

        try await transport.waitUntilSent { sent in sent.filter { $0 == .binary(Self.frame) }.count == 2 }
    }

    @Test func serverEventsDriveTheCallScreenAndThePlayer() async throws {
        let (_, model) = try await connectedCall()
        let agentAudio = Data(repeating: 1, count: 960)

        transport.serverSends(#"{"type":"turn.user_end","reason":"vad"}"#)
        await waitUntil { model.callActivity == .thinking }
        #expect(model.callActivity == .thinking)

        transport.serverSends(#"{"type":"transcript","role":"user","text":"hello"}"#)
        transport.serverSends(#"{"type":"turn.agent_start"}"#)
        transport.serverSends(binary: agentAudio)
        await waitUntil { audio.played.count == 1 }
        #expect(model.callActivity == .agentSpeaking)
        #expect(audio.played == [agentAudio])

        transport.serverSends(#"{"type":"turn.agent_end"}"#)
        await waitUntil { model.callActivity == .listening }
        #expect(audio.turnEnds == 1)
        #expect(model.callActivity == .listening)
    }

    @Test func nonFatalServerErrorKeepsTheCall() async throws {
        let (_, model) = try await connectedCall()
        transport.serverSends(#"{"type":"turn.user_end","reason":"vad"}"#)
        await waitUntil { model.callActivity == .thinking }

        transport.serverSends(#"{"type":"error","code":"stt_failed","message":"STT timed out","fatal":false}"#)

        await waitUntil { model.callActivity == .listening }
        #expect(model.callActivity == .listening)
        #expect(callKit.ends.isEmpty)
        #expect(audio.isRunning)
    }

    // MARK: - Mute

    @Test func muteFromCallKitGoesToTheAudioAndTheServer() async throws {
        // Keep the coordinator alive: CallKit (and the fake) hold their delegate weakly.
        let (coordinator, _) = try await connectedCall()
        defer { withExtendedLifetime(coordinator) {} }

        callKit.userSetsMuted(true)
        audio.microphone(Self.frame)  // the real AudioIO would not deliver it; the session drops it too
        callKit.userSetsMuted(false)

        #expect(audio.mutes.suffix(2) == [true, false])
        try await transport.waitUntilSent { sent in
            sent.contains(.text(#"{"muted":true,"type":"mute"}"#)) && sent.contains(.text(#"{"muted":false,"type":"mute"}"#))
        }
        #expect(!transport.sentBinaries.contains(Self.frame))
    }

    @Test func muteBeforeTheSocketOpensIsSentRightAfterReady() async throws {
        let (_, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }

        callKit.userSetsMuted(true)
        callKit.activateAudio()
        try await transport.waitUntilSent { $0.count == 1 }
        transport.serverSends(Self.ready)

        let sent = try await transport.waitUntilSent { $0.count == 2 }
        #expect(sent[1] == .text(#"{"muted":true,"type":"mute"}"#))
        await waitUntil { audio.isRunning }
        #expect(audio.mutes.contains(true))
        #expect(audio.mutes.last == true)
    }

    // MARK: - Ends (Review Focus 1, 2, 3)

    @Test func userEndsFromTheSystemUIWhileTheAgentSpeaks() async throws {
        let (_, model) = try await connectedCall()
        transport.serverSends(#"{"type":"turn.agent_start"}"#)
        transport.serverSends(binary: Data(count: 960))
        await waitUntil { audio.played.count == 1 }

        // The agent goes on talking. Holding the main actor (no await) lets the session hand this
        // frame to the coordinator, whose event loop then waits for the main actor: it is already
        // on its way when the user hangs up.
        transport.serverSends(binary: Data(count: 960))
        usleep(50_000)
        callKit.userEndsCall()
        // More of the turn arrives before the background `session.end` closes the socket.
        transport.serverSends(binary: Data(count: 960))
        transport.serverSends(#"{"type":"turn.agent_end"}"#)

        // Audio stops and the screen goes Home right away, without waiting for the network.
        #expect(!audio.isRunning)
        #expect(model.phase == .ready(info))
        #expect(model.message == nil)
        let sent = try await transport.waitUntilSent { $0.last == .close(1000) }
        #expect(sent.suffix(2) == [.text(#"{"type":"session.end"}"#), .close(1000)])
        #expect(callKit.ends.isEmpty)  // CallKit already ended it
        // Nothing that arrived around the end reaches the player.
        try await Task.sleep(for: .milliseconds(50))
        #expect(audio.played.count == 1)
        #expect(audio.turnEnds == 0)
        #expect(!audio.isRunning)
    }

    @Test func endButtonAsksCallKitAndEndsTheSameWay() async throws {
        let (coordinator, model) = try await connectedCall()
        let id = try #require(coordinator.currentCallID)

        model.endCall()

        await waitUntil { model.phase == .ready(info) }
        #expect(callKit.endRequests == [id])
        #expect(!audio.isRunning)
        try await transport.waitUntilSent { $0.suffix(2) == [.text(#"{"type":"session.end"}"#), .close(1000)] }
        #expect(callKit.ends.isEmpty)
    }

    @Test func connectionLostEndsTheCallAsFailed() async throws {
        let (_, model) = try await connectedCall()

        transport.serverCloses(code: nil)

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(model.message == "Connection lost")
        #expect(!audio.isRunning)
        #expect(audio.stops >= 1)
    }

    /// No network: CallKit activates the audio, but the server never answers.
    @Test func serverThatDoesNotAnswerEndsWithoutTheFailedCallAlert() async throws {
        let unreachable = FakeTransport(connectError: .connectionFailed("The operation timed out."))
        let (coordinator, model) = try await makeCoordinator(transport: unreachable)
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }

        callKit.activateAudio()

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(callKit.ends.allSatisfy { $0.cause.callKitReason != .failed })
        #expect(model.message == "Connection lost")
        #expect(coordinator.currentCallID == nil)
        #expect(!audio.isRunning)
    }

    @Test func fatalServerErrorEndsTheCallAsFailed() async throws {
        let (_, model) = try await connectedCall()

        transport.serverSends(#"{"type":"error","code":"internal","message":"boom","fatal":true}"#)
        transport.serverCloses(code: 4400)

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(model.message == "Call failed (internal).")
    }

    @Test func serverNormalCloseIsRemoteEnded() async throws {
        let (_, model) = try await connectedCall()

        transport.serverCloses(code: 1000)

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.remoteEnded))
        #expect(model.message == nil)
    }

    @Test func revokedTokenClearsCredentialsAndGoesToPairing() async throws {
        let (_, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }
        callKit.activateAudio()
        try await transport.waitUntilSent { $0.count == 1 }

        transport.serverCloses(code: 4401)

        await waitUntil { model.phase == .unpaired }
        #expect(model.phase == .unpaired)
        #expect(model.message == AppModel.Message.revoked)
        #expect(try store.load() == nil)
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(audio.playbackRates.isEmpty)
    }

    @Test func callKitRefusalReturnsHomeWithAMessage() async throws {
        callKit.startError = FakeCallKitError()
        let (coordinator, model) = try await makeCoordinator()

        model.startCall()

        await waitUntil { model.phase == .ready(info) }
        #expect(model.message == AppModel.Message.callNotStarted)
        #expect(callKit.ends.isEmpty)
        #expect(coordinator.currentCallID == nil)
        #expect(transportsMade.all.isEmpty)
    }

    @Test func audioNeverActivatedIsUnanswered() async throws {
        let (coordinator, model) = try await makeCoordinator(activationTimeout: .milliseconds(50))

        model.startCall()

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.unanswered))
        #expect(model.message == AppModel.Message.callNotStarted)
        #expect(coordinator.currentCallID == nil)
        // A late activation opens nothing.
        callKit.activateAudio()
        try await Task.sleep(for: .milliseconds(50))
        #expect(transportsMade.all.isEmpty)
    }

    @Test func startAcceptedAfterTheActivationTimeoutIsReportedEnded() async throws {
        callKit.holdsStart = true
        let (coordinator, model) = try await makeCoordinator(activationTimeout: .milliseconds(50))
        model.startCall()
        await waitUntil { model.phase == .ready(info) }
        let start = try #require(callKit.starts.first)
        try #require(callKit.ends == [FakeCallControl.End(id: start.id, cause: .unanswered)])

        callKit.completeStart()

        await waitUntil { callKit.ends.count == 2 }
        #expect(callKit.ends.last == FakeCallControl.End(id: start.id, cause: .failed))
        #expect(coordinator.currentCallID == nil)
        #expect(model.phase == .ready(info))
    }

    @Test func startAcceptedAfterTheUserEndedIsReportedEnded() async throws {
        callKit.holdsStart = true
        let (coordinator, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.isStartPending }
        try #require(callKit.isStartPending)

        model.endCall()
        await waitUntil { model.phase == .ready(info) }
        try #require(coordinator.currentCallID == nil)
        try #require(callKit.ends.isEmpty)
        callKit.completeStart()

        await waitUntil { !callKit.ends.isEmpty }
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(transportsMade.all.isEmpty)
    }

    @Test func endRefusedByCallKitIsReportedEndedAndReturnsHome() async throws {
        let (coordinator, model) = try await connectedCall()
        callKit.endError = FakeCallKitError()

        model.endCall()

        await waitUntil { model.phase == .ready(info) }
        #expect(model.phase == .ready(info))
        #expect(model.message == nil)
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(!audio.isRunning)
        #expect(coordinator.currentCallID == nil)
        try await transport.waitUntilSent { $0.last == .close(1000) }
    }

    @Test func microphoneFailureEndsTheCallAsFailed() async throws {
        audio.startError = AudioIOError.microphoneUnavailable
        let (_, model) = try await makeCoordinator()
        model.startCall()
        await waitUntil { callKit.starts.count == 1 }
        callKit.activateAudio()
        try await transport.waitUntilSent { $0.count == 1 }

        transport.serverSends(Self.ready)

        await waitUntil { model.phase == .ready(info) }
        #expect(try callKit.ends == endReportedForTheStartedCall(.failed))
        #expect(model.message == AppModel.Message.microphoneUnavailable)
        try await transport.waitUntilSent { $0.last == .close(1000) }
    }

    @Test func callKitResetEndsTheCall() async throws {
        let (coordinator, model) = try await connectedCall()

        callKit.reset()

        #expect(model.phase == .ready(info))
        #expect(!audio.isRunning)
        #expect(coordinator.currentCallID == nil)
        try await transport.waitUntilSent { $0.last == .close(1000) }
    }

    @Test func aNewCallWorksAfterTheEnd() async throws {
        let (coordinator, model) = try await connectedCall()
        callKit.userEndsCall()
        let first = callKit.starts.first?.id

        model.startCall()

        await waitUntil { callKit.starts.count == 2 }
        #expect(callKit.starts.count == 2)
        #expect(coordinator.currentCallID != first)
    }
}
