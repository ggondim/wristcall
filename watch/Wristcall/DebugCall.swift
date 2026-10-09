#if DEBUG
import AVFAudio
import Foundation
import os
import WristcallKit

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
/// - `-autoCallAgent <slug>`: the agent `-autoCall` calls (the first agent with that slug, once its
///   server has answered); without it, the first agent.
/// - `-autoCallDelay <seconds>`: waits that long on the Home screen before tapping "Call"
///   (time to run `wristcall devices revoke` and see the 4401 path).
/// - `-endCallAfter <seconds>`: taps "End" that many seconds after the call started.
/// - `-showOptions <slug>`: opens the call options of that agent on Home (once its server has
///   answered), as a long press on it does.
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
        let slug = value(after: "-autoCallAgent", in: arguments)
        // The agent may be on a server that answers after the first one.
        let isListed = { model.agents.contains { $0.agent.slug == slug } }
        for _ in 0..<100 where slug != nil && model.isLoadingServers && !isListed() {
            try? await Task.sleep(for: .milliseconds(100))
        }
        guard model.canCall else { return }
        if let delay = seconds(after: "-autoCallDelay", in: arguments) {
            try? await Task.sleep(for: .seconds(delay))
        }
        if let slug {
            // Never another agent in its place (decision W4).
            guard let target = model.agents.first(where: { $0.agent.slug == slug }) else { return }
            model.startCall(target)
        } else {
            model.startCall()
        }
        guard let seconds = seconds(after: "-endCallAfter", in: arguments) else { return }
        try? await Task.sleep(for: .seconds(seconds))
        model.endCall()
    }

    /// The agent whose options `-showOptions` asks to open; `nil` without the flag or when no listed
    /// agent has that slug within 10 s.
    @MainActor
    static func optionsTarget(_ model: AppModel, arguments: [String]) async -> AgentTarget? {
        guard let slug = value(after: "-showOptions", in: arguments) else { return nil }
        for _ in 0..<100 {
            if let target = model.agents.first(where: { $0.agent.slug == slug }) { return target }
            try? await Task.sleep(for: .milliseconds(100))
        }
        return nil
    }

    private static func seconds(after flag: String, in arguments: [String]) -> Double? {
        value(after: flag, in: arguments).flatMap(Double.init)
    }

    private static func value(after flag: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: flag), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}

/// `-openURL <url>` (Debug builds only): hands `url` to what `onOpenURL` does, because
/// `xcrun simctl openurl` fails on the watch simulator (LaunchServices error 115). Everything after
/// the system's delivery of the URL runs as for a tapped complication:
///
///     xcrun simctl launch <device> <bundle id> -noCallKit -syntheticMic \
///         -openURL 'wristcall://call?agent=<serverID>/<agentID>'
enum DebugShortcut {
    @MainActor
    static func run(arguments: [String]) {
        guard let index = arguments.firstIndex(of: "-openURL"), arguments.indices.contains(index + 1),
              let url = URL(string: arguments[index + 1])
        else { return }
        ShortcutCalls.request(from: url, store: PendingCallStore())
    }
}
#endif
