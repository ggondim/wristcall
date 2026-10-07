import Foundation
import os
import WristcallKit

/// Runs a call: CallKit (`CallControlling`), the audio (`CallAudio`) and the protocol
/// (`CallSession` over a `CallTransport`), and reports the outcome to `AppModel`.
///
/// Order of a call: "Call" → CallKit start → `callControllerDidActivateAudio()` → socket and
/// `session.start` (never before: TN3135) → `session.ready` → CallKit "connected", microphone
/// and player start → server events drive the call screen → end.
///
/// Ends:
/// - by the user (system UI or the app's "End", which asks CallKit): audio stops, the screen goes
///   Home, `session.end` + close 1000 go out in the background. CallKit already knows; no report.
/// - by the server or the network: CallKit gets `reportEnded` (`.remoteEnded` for a normal close,
///   `.failed` otherwise) and `AppModel.callDidEnd(_:)` picks the message ("Connection lost",
///   pairing again after 4401...).
/// - before the call exists (CallKit refused, audio never activated, no microphone):
///   `AppModel.callDidFail(message:)`.
@MainActor
final class CallCoordinator: CallHandling {
    typealias TransportFactory = (Credentials) throws -> any CallTransport

    /// How long CallKit gets to activate the audio session after the start request.
    static let defaultActivationTimeout: Duration = .seconds(10)
    /// Caller name when the server has no profile (never expected; `/v1/me` always lists one).
    static let fallbackDisplayName = "wristcall"
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "call")

    weak var model: AppModel?

    private let callControl: any CallControlling
    private let audio: any CallAudio
    private let makeTransport: TransportFactory
    private let activationTimeout: Duration
    private var call: ActiveCall?

    private final class ActiveCall {
        let id = UUID()
        let request: CallRequest
        var session: CallSession?
        var muted = false
        /// Agent audio frames received in the current agent turn (for the log).
        var agentFrames = 0
        var tasks: [Task<Void, Never>] = []

        init(request: CallRequest) {
            self.request = request
        }

        func cancelTasks() {
            tasks.forEach { $0.cancel() }
            tasks = []
        }
    }

    init(
        callControl: any CallControlling,
        audio: any CallAudio,
        activationTimeout: Duration = CallCoordinator.defaultActivationTimeout,
        makeTransport: @escaping TransportFactory = { credentials in
            try NWWebSocketTransport(server: credentials.serverURL, token: credentials.token)
        }
    ) {
        self.callControl = callControl
        self.audio = audio
        self.activationTimeout = activationTimeout
        self.makeTransport = makeTransport
        callControl.delegate = self
    }

    /// The id CallKit knows the current call by; `nil` between calls.
    var currentCallID: UUID? { call?.id }

    // MARK: - CallHandling

    func startCall(_ request: CallRequest) {
        guard call == nil else { return }
        let call = ActiveCall(request: request)
        self.call = call
        do {
            try audio.prepare()
        } catch {
            Self.log.error("audio session category: \(error.localizedDescription, privacy: .public)")
        }
        let id = call.id
        let name = request.profile?.displayName ?? Self.fallbackDisplayName
        call.tasks.append(Task { [weak self] in
            do {
                try await self?.callControl.startCall(id: id, displayName: name)
            } catch {
                self?.failBeforeConnecting(id, message: AppModel.Message.callNotStarted, report: nil)
                return
            }
            // The call was released while the request was in flight (activation timeout, or
            // End tapped): CallKit has just registered it and nobody else will end it.
            guard let self, self.call?.id != id else { return }
            Self.log.notice("CallKit accepted a call that already ended")
            self.callControl.reportEnded(id: id, cause: .failed)
        })
        call.tasks.append(Task { [weak self, activationTimeout] in
            try? await Task.sleep(for: activationTimeout)
            guard !Task.isCancelled, let self, self.call?.id == id, self.call?.session == nil else { return }
            Self.log.error("CallKit did not activate the audio session")
            self.failBeforeConnecting(id, message: AppModel.Message.callNotStarted, report: .unanswered)
        })
    }

    func endCall() {
        guard let id = call?.id else { return }
        Task { [weak self] in
            do {
                // CallKit runs the end action, which comes back as callControllerDidEndCall(_:).
                try await self?.callControl.endCall(id: id)
            } catch {
                // CallKit refused the end action: report the call ended so it does not linger,
                // then end it here.
                Self.log.error("CallKit refused to end the call: \(error.localizedDescription, privacy: .public)")
                self?.callControl.reportEnded(id: id, cause: .failed)
                self?.endByUser(id)
            }
        }
    }

    // MARK: - Session

    private func openSession(for call: ActiveCall) {
        let transport: any CallTransport
        do {
            transport = try makeTransport(call.request.credentials)
        } catch {
            Self.log.error("invalid server URL: \(error.localizedDescription, privacy: .public)")
            failBeforeConnecting(call.id, message: AppModel.Message.connectionLost, report: .failed)
            return
        }
        let session = CallSession(transport: transport)
        call.session = session
        if call.muted {
            session.setMuted(true)
        }
        let id = call.id
        let profile = call.request.profile?.name
        let turnEnd = call.request.turnEnd
        call.tasks.append(Task { [weak self] in
            for await event in session.events {
                self?.handle(event, callID: id)
            }
        })
        call.tasks.append(Task { [weak self] in
            // Failures arrive as `.ended` on the event stream.
            guard let ready = try? await session.start(profile: profile, turnEnd: turnEnd) else { return }
            self?.sessionReady(ready, callID: id)
        })
    }

    private func sessionReady(_ ready: SessionReady, callID id: UUID) {
        guard let call, call.id == id, let session = call.session else { return }
        callControl.reportConnected(id: id)
        audio.setMuted(call.muted)
        do {
            try audio.start(playbackSampleRate: ready.audioOut.sampleRate, onFrame: Self.frameSink(session))
        } catch {
            Self.log.error("audio did not start: \(error.localizedDescription, privacy: .public)")
            finish(id, cause: .failed) { $0.callDidFail(message: AppModel.Message.microphoneUnavailable) }
            return
        }
        model?.callActivityDidChange(.listening)
    }

    /// Built outside the main actor: the microphone tap calls it on the audio thread.
    private nonisolated static func frameSink(_ session: CallSession) -> @Sendable (Data) -> Void {
        { frame in session.sendAudio(frame) }
    }

    private func handle(_ event: CallEvent, callID id: UUID) {
        guard let call, call.id == id else { return }
        switch event {
        case .userTurnEnded(let reason):
            Self.log.notice("user turn ended: \(reason.wireValue, privacy: .public)")
            model?.callActivityDidChange(.thinking)
        case .agentTurnStarted:
            Self.log.notice("agent turn started")
            call.agentFrames = 0
            model?.callActivityDidChange(.agentSpeaking)
        case .agentAudio(let data):
            call.agentFrames += 1
            audio.play(data)
        case .agentTurnEnded:
            Self.log.notice("agent turn ended: \(call.agentFrames) audio frames")
            audio.agentTurnEnded()
            model?.callActivityDidChange(.listening)
        case .transcript:
            // Informational; not shown in the MVP (and never logged: it is the user's speech).
            break
        case .error(let code, _, let fatal):
            Self.log.error("server error \(code.wireValue, privacy: .public) fatal=\(fatal)")
            if !fatal {
                model?.callActivityDidChange(.listening)
            }
        case .ended(let reason):
            Self.log.notice("call ended by the server or the network: \(String(describing: reason), privacy: .public)")
            finish(id, cause: reason == .normal ? .remoteEnded : .failed) { $0.callDidEnd(reason) }
        }
    }

    // MARK: - Ends

    /// The user hung up (system UI, the app's "End" through CallKit, or a CallKit reset).
    private func endByUser(_ id: UUID) {
        guard let call, call.id == id else { return }
        Self.log.notice("call ended by the user")
        let session = call.session
        release(call)
        model?.callDidEnd(.normal)
        if let session {
            // session.end, then close 1000; at most CallSession.endFlushTimeout.
            Task { await session.end() }
        }
    }

    /// The call cannot go on: tell CallKit (`cause`) and the model (`notify`), close what is open.
    private func finish(_ id: UUID, cause: CallEndCause, notify: (AppModel) -> Void) {
        guard let call, call.id == id else { return }
        let session = call.session
        release(call)
        callControl.reportEnded(id: id, cause: cause)
        if let model { notify(model) }
        if let session {
            Task { await session.end() }
        }
    }

    /// Before `session.ready`: CallKit refused the call (`report == nil`, nothing to report),
    /// the audio never activated, or the transport could not be built.
    private func failBeforeConnecting(_ id: UUID, message: String, report cause: CallEndCause?) {
        guard let call, call.id == id else { return }
        let session = call.session
        release(call)
        if let cause {
            callControl.reportEnded(id: id, cause: cause)
        }
        model?.callDidFail(message: message)
        if let session {
            Task { await session.end() }
        }
    }

    /// Stops the microphone and the player first, then forgets the call.
    private func release(_ call: ActiveCall) {
        audio.stop()
        call.cancelTasks()
        self.call = nil
    }
}

extension CallCoordinator: CallControllerDelegate {
    func callControllerDidActivateAudio() {
        guard let call, call.session == nil else { return }
        openSession(for: call)
    }

    func callControllerDidDeactivateAudio() {
        // After the end the audio is already stopped. During a call it means the system took the
        // session away: the microphone and the player cannot go on.
        audio.stop()
    }

    func callControllerDidSetMuted(_ muted: Bool, callID: UUID) {
        guard let call, call.id == callID else { return }
        call.muted = muted
        // Audio first: on mute it hands the last words to the session before `mute` goes out.
        audio.setMuted(muted)
        call.session?.setMuted(muted)
    }

    func callControllerDidEndCall(_ callID: UUID) {
        endByUser(callID)
    }

    func callControllerDidReset() {
        guard let id = call?.id else { return }
        endByUser(id)
    }
}
