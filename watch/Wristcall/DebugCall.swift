#if DEBUG
import AVFAudio
import Foundation
import os

/// Simulator stand-in for CallKit (Debug builds only, `-noCallKit`).
///
/// In the watch simulator CallKit accepts the start request but never activates the audio session
/// (`provider(_:didActivate:)` does not arrive). This control activates the session itself and
/// calls the same `CallControllerDelegate` methods, so the coordinator runs the real path:
/// socket after activation, `session.start`, audio, ends. No system call UI and no system mute.
@MainActor
final class DirectAudioCallControl: CallControlling {
    weak var delegate: (any CallControllerDelegate)?
    private let session: AVAudioSession
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "callkit")

    init(session: AVAudioSession = .sharedInstance()) {
        self.session = session
    }

    func startCall(id: UUID, displayName: String) async throws {
        Self.log.notice("debug call without CallKit: activating the audio session")
        _ = try await session.activate(options: [])
        delegate?.callControllerDidActivateAudio()
    }

    func endCall(id: UUID) async throws {
        delegate?.callControllerDidEndCall(id)
        deactivate()
    }

    func reportConnected(id: UUID) {
        Self.log.notice("debug call connected")
    }

    func reportEnded(id: UUID, cause: CallEndCause) {
        Self.log.notice("debug call ended: \(String(describing: cause), privacy: .public)")
        deactivate()
    }

    private func deactivate() {
        try? session.setActive(false, options: .notifyOthersOnDeactivation)
        delegate?.callControllerDidDeactivateAudio()
    }
}

/// Launch arguments that drive a call in the simulator (Debug builds only):
///
///     xcrun simctl launch <device> <bundle id> -noCallKit -syntheticMic -autoCall -endCallAfter 20
///
/// - `-noCallKit`: `DirectAudioCallControl` instead of `CallController`.
/// - `-syntheticMic`: a generated tone instead of the microphone (the simulator has none).
/// - `-autoCall`: taps "Call" as soon as the Home screen can call (within 10 s of launch).
/// - `-autoCallDelay <seconds>`: waits that long on the Home screen before tapping "Call"
///   (time to run `wristcall devices revoke` and see the 4401 path).
/// - `-endCallAfter <seconds>`: taps "End" that many seconds after the call started.
enum DebugCall {
    static func usesCallKit(_ arguments: [String]) -> Bool {
        !arguments.contains("-noCallKit")
    }

    static func usesSyntheticMic(_ arguments: [String]) -> Bool {
        arguments.contains("-syntheticMic")
    }

    @MainActor
    static func run(_ model: AppModel, arguments: [String]) async {
        guard arguments.contains("-autoCall") else { return }
        for _ in 0..<100 where !model.canCall {
            try? await Task.sleep(for: .milliseconds(100))
        }
        guard model.canCall else { return }
        if let delay = seconds(after: "-autoCallDelay", in: arguments) {
            try? await Task.sleep(for: .seconds(delay))
        }
        model.startCall()
        guard let seconds = seconds(after: "-endCallAfter", in: arguments) else { return }
        try? await Task.sleep(for: .seconds(seconds))
        model.endCall()
    }

    private static func seconds(after flag: String, in arguments: [String]) -> Double? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return Double(arguments[index + 1])
    }
}
#endif
