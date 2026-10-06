import AVFAudio
import CallKit
import Foundation
import os

/// Why the app ends a call by itself, without an end action from the user.
enum CallEndCause: Equatable, Sendable {
    /// The server closed the call normally (1000).
    case remoteEnded
    /// Connection lost, server error, revoked token or no microphone.
    case failed
    /// CallKit never activated the audio session.
    case unanswered

    var callKitReason: CXCallEndedReason {
        switch self {
        case .remoteEnded: .remoteEnded
        case .failed: .failed
        case .unanswered: .unanswered
        }
    }
}

/// What CallKit tells the coordinator. Always called on the main actor.
@MainActor
protocol CallControllerDelegate: AnyObject {
    /// CallKit activated the audio session: the socket and the microphone may start now (TN3135).
    func callControllerDidActivateAudio()
    /// CallKit deactivated the audio session.
    func callControllerDidDeactivateAudio()
    /// The user tapped mute or unmute in the system call UI.
    func callControllerDidSetMuted(_ muted: Bool, callID: UUID)
    /// An end action ran for `callID`: the user hung up in the system UI, or `endCall(id:)` was requested.
    /// CallKit already considers the call over; do not report it ended again.
    func callControllerDidEndCall(_ callID: UUID)
    /// CallKit dropped every call. Clean up as if the call ended.
    func callControllerDidReset()
}

/// The CallKit side of a call as the coordinator sees it: `CallController` in the app,
/// a fake in the tests (and `DirectAudioCallControl` in the simulator, Debug only).
@MainActor
protocol CallControlling: AnyObject {
    var delegate: (any CallControllerDelegate)? { get set }
    /// Asks the system for an outgoing call shown as `displayName`. Throws if CallKit refuses it.
    func startCall(id: UUID, displayName: String) async throws
    /// Asks the system to end `id`; `callControllerDidEndCall(_:)` follows. Throws if CallKit
    /// does not know the call.
    func endCall(id: UUID) async throws
    /// `session.ready` arrived: the call is connected.
    func reportConnected(id: UUID)
    /// The call ended without an end action (the server or a failure ended it).
    func reportEnded(id: UUID, cause: CallEndCause)
}

/// The reports the app makes to its `CXProvider`. Lets the tests record them.
@MainActor
protocol CallReporting: AnyObject {
    func reportOutgoingCall(with callID: UUID, startedConnectingAt date: Date?)
    func reportOutgoingCall(with callID: UUID, connectedAt date: Date?)
    func reportCall(with callID: UUID, endedAt date: Date?, reason: CXCallEndedReason)
    func reportCall(with callID: UUID, updated update: CXCallUpdate)
}

/// The transactions the app asks of `CXCallController`. Lets the tests record them.
@MainActor
protocol CallRequesting: AnyObject {
    func requestTransaction(_ transaction: CXTransaction) async throws
}

extension CXProvider: CallReporting {}

extension CXCallController: CallRequesting {
    func requestTransaction(_ transaction: CXTransaction) async throws {
        try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
            request(transaction) { error in
                if let error {
                    continuation.resume(throwing: error)
                } else {
                    continuation.resume()
                }
            }
        }
    }
}

/// CallKit for wristcall: one outgoing audio call at a time, shown with the profile's name.
///
/// Owns the `CXProvider` (delegate callbacks on the main queue) and the `CXCallController`.
/// It translates CallKit into `CallControllerDelegate` calls and does nothing else: the
/// coordinator decides what each event means. Lives as long as the app (the provider is
/// never invalidated).
@MainActor
final class CallController: NSObject, CallControlling {
    weak var delegate: (any CallControllerDelegate)?

    private let reporter: any CallReporting
    private let requester: any CallRequesting
    private let provider: CXProvider?
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "callkit")

    static func makeConfiguration() -> CXProviderConfiguration {
        let configuration = CXProviderConfiguration()
        configuration.supportsVideo = false
        configuration.maximumCallGroups = 1
        configuration.maximumCallsPerCallGroup = 1
        configuration.supportedHandleTypes = [.generic]
        configuration.includesCallsInRecents = false
        return configuration
    }

    /// The real CallKit.
    override convenience init() {
        let provider = CXProvider(configuration: Self.makeConfiguration())
        self.init(reporter: provider, requester: CXCallController(), provider: provider)
    }

    /// `provider` receives `self` as delegate; tests pass `nil` and call the delegate methods directly.
    init(reporter: any CallReporting, requester: any CallRequesting, provider: CXProvider? = nil) {
        self.reporter = reporter
        self.requester = requester
        self.provider = provider
        super.init()
        provider?.setDelegate(self, queue: nil)
    }

    // MARK: - CallControlling

    func startCall(id: UUID, displayName: String) async throws {
        let action = CXStartCallAction(call: id, handle: CXHandle(type: .generic, value: displayName))
        action.isVideo = false
        do {
            try await requester.requestTransaction(CXTransaction(action: action))
        } catch {
            Self.log.error("start call refused: \(error.localizedDescription, privacy: .public)")
            throw error
        }
    }

    func endCall(id: UUID) async throws {
        try await requester.requestTransaction(CXTransaction(action: CXEndCallAction(call: id)))
    }

    func reportConnected(id: UUID) {
        reporter.reportOutgoingCall(with: id, connectedAt: nil)
    }

    func reportEnded(id: UUID, cause: CallEndCause) {
        reporter.reportCall(with: id, endedAt: nil, reason: cause.callKitReason)
    }
}

extension CallController: @preconcurrency CXProviderDelegate {
    func providerDidReset(_ provider: CXProvider) {
        Self.log.notice("provider reset")
        delegate?.callControllerDidReset()
    }

    func provider(_ provider: CXProvider, perform action: CXStartCallAction) {
        let update = CXCallUpdate()
        update.remoteHandle = action.handle
        update.localizedCallerName = action.handle.value
        update.hasVideo = false
        update.supportsHolding = false
        update.supportsGrouping = false
        update.supportsUngrouping = false
        update.supportsDTMF = false
        reporter.reportCall(with: action.callUUID, updated: update)
        reporter.reportOutgoingCall(with: action.callUUID, startedConnectingAt: nil)
        action.fulfill()
    }

    func provider(_ provider: CXProvider, perform action: CXEndCallAction) {
        action.fulfill()
        delegate?.callControllerDidEndCall(action.callUUID)
    }

    func provider(_ provider: CXProvider, perform action: CXSetMutedCallAction) {
        // Without fulfill() the system button flips back.
        action.fulfill()
        delegate?.callControllerDidSetMuted(action.isMuted, callID: action.callUUID)
    }

    func provider(_ provider: CXProvider, timedOutPerforming action: CXAction) {
        Self.log.error("CallKit action timed out: \(type(of: action), privacy: .public)")
    }

    func provider(_ provider: CXProvider, didActivate audioSession: AVAudioSession) {
        Self.log.notice("audio session activated")
        delegate?.callControllerDidActivateAudio()
    }

    func provider(_ provider: CXProvider, didDeactivate audioSession: AVAudioSession) {
        Self.log.notice("audio session deactivated")
        delegate?.callControllerDidDeactivateAudio()
    }
}
