import CallKit
import Foundation
@testable import Wristcall

/// Records what `CallController` reports to its provider.
@MainActor
final class FakeCallReporter: CallReporting {
    enum Report: Equatable {
        case startedConnecting(UUID)
        case connected(UUID)
        case ended(UUID, CXCallEndedReason)
        case updated(UUID, callerName: String?, video: Bool, holding: Bool, dtmf: Bool)
    }

    private(set) var reports: [Report] = []

    func reportOutgoingCall(with callID: UUID, startedConnectingAt date: Date?) {
        reports.append(.startedConnecting(callID))
    }

    func reportOutgoingCall(with callID: UUID, connectedAt date: Date?) {
        reports.append(.connected(callID))
    }

    func reportCall(with callID: UUID, endedAt date: Date?, reason: CXCallEndedReason) {
        reports.append(.ended(callID, reason))
    }

    func reportCall(with callID: UUID, updated update: CXCallUpdate) {
        reports.append(.updated(
            callID,
            callerName: update.localizedCallerName,
            video: update.hasVideo,
            holding: update.supportsHolding,
            dtmf: update.supportsDTMF
        ))
    }
}

/// Records the transactions `CallController` asks for; fails them with `error` if set.
@MainActor
final class FakeCallRequester: CallRequesting {
    private(set) var transactions: [CXTransaction] = []
    var error: (any Error)?

    func requestTransaction(_ transaction: CXTransaction) async throws {
        transactions.append(transaction)
        if let error { throw error }
    }
}

/// Records what `CallController` forwards to the coordinator.
@MainActor
final class RecordingCallControllerDelegate: CallControllerDelegate {
    enum Event: Equatable {
        case activated
        case deactivated
        case muted(Bool, UUID)
        case ended(UUID)
        case reset
    }

    private(set) var events: [Event] = []

    func callControllerDidActivateAudio() { events.append(.activated) }
    func callControllerDidDeactivateAudio() { events.append(.deactivated) }
    func callControllerDidSetMuted(_ muted: Bool, callID: UUID) { events.append(.muted(muted, callID)) }
    func callControllerDidEndCall(_ callID: UUID) { events.append(.ended(callID)) }
    func callControllerDidReset() { events.append(.reset) }
}

/// Stands in for CallKit in the coordinator tests (task 10). `endCall(id:)` behaves like
/// CallKit: it runs the end action, so `callControllerDidEndCall(_:)` follows.
@MainActor
final class FakeCallControl: CallControlling {
    struct Start: Equatable {
        let id: UUID
        let displayName: String
    }

    weak var delegate: (any CallControllerDelegate)?
    var startError: (any Error)?
    var endError: (any Error)?

    private(set) var starts: [Start] = []
    private(set) var endRequests: [UUID] = []
    private(set) var connected: [UUID] = []
    private(set) var ended: [CallEndCause] = []

    var callID: UUID? { starts.last?.id }

    func startCall(id: UUID, displayName: String) async throws {
        starts.append(Start(id: id, displayName: displayName))
        if let startError { throw startError }
    }

    func endCall(id: UUID) async throws {
        endRequests.append(id)
        if let endError { throw endError }
        delegate?.callControllerDidEndCall(id)
    }

    func reportConnected(id: UUID) {
        connected.append(id)
    }

    func reportEnded(id: UUID, cause: CallEndCause) {
        ended.append(cause)
    }

    // MARK: - Playing the system

    func activateAudio() { delegate?.callControllerDidActivateAudio() }
    func deactivateAudio() { delegate?.callControllerDidDeactivateAudio() }
    func userSetsMuted(_ muted: Bool) { delegate?.callControllerDidSetMuted(muted, callID: callID!) }
    func userEndsCall() { delegate?.callControllerDidEndCall(callID!) }
    func reset() { delegate?.callControllerDidReset() }
}

struct FakeCallKitError: Error, Equatable {}

/// CallKit actions that remember `fulfill()`. Outside a real transaction `isComplete` stays
/// `false` even after `fulfill()`, so the tests check this flag instead.
final class SpyStartCallAction: CXStartCallAction {
    private(set) var fulfilled = false

    override func fulfill() {
        fulfilled = true
        super.fulfill()
    }
}

final class SpyEndCallAction: CXEndCallAction {
    private(set) var fulfilled = false

    override func fulfill() {
        fulfilled = true
        super.fulfill()
    }
}

final class SpyMutedCallAction: CXSetMutedCallAction {
    private(set) var fulfilled = false

    override func fulfill() {
        fulfilled = true
        super.fulfill()
    }
}
