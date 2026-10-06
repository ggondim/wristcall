import AVFAudio
import CallKit
import Foundation
import Testing
@testable import Wristcall

@MainActor
struct CallControllerTests {
    let reporter = FakeCallReporter()
    let requester = FakeCallRequester()
    let delegate = RecordingCallControllerDelegate()
    let id = UUID()

    func makeController() -> CallController {
        let controller = CallController(reporter: reporter, requester: requester)
        controller.delegate = delegate
        return controller
    }

    /// A real provider, only to pass to the delegate methods (they report through `reporter`).
    func withProvider(_ body: (CXProvider) -> Void) {
        let provider = CXProvider(configuration: CallController.makeConfiguration())
        body(provider)
        provider.invalidate()
    }

    @Test func configurationIsOneAudioCallWithoutRecents() {
        let configuration = CallController.makeConfiguration()
        #expect(configuration.supportsVideo == false)
        #expect(configuration.maximumCallGroups == 1)
        #expect(configuration.maximumCallsPerCallGroup == 1)
        #expect(configuration.supportedHandleTypes == [.generic])
        #expect(configuration.includesCallsInRecents == false)
    }

    @Test func startCallRequestsAnAudioCallToTheProfileName() async throws {
        try await makeController().startCall(id: id, displayName: "Agent")

        let transaction = try #require(requester.transactions.first)
        #expect(requester.transactions.count == 1)
        let action = try #require(transaction.actions.first as? CXStartCallAction)
        #expect(transaction.actions.count == 1)
        #expect(action.callUUID == id)
        #expect(action.handle.type == .generic)
        #expect(action.handle.value == "Agent")
        #expect(action.isVideo == false)
    }

    @Test func refusedStartIsThrown() async {
        requester.error = FakeCallKitError()
        await #expect(throws: FakeCallKitError.self) {
            try await makeController().startCall(id: id, displayName: "Agent")
        }
    }

    @Test func endCallRequestsAnEndAction() async throws {
        try await makeController().endCall(id: id)

        let action = try #require(requester.transactions.first?.actions.first as? CXEndCallAction)
        #expect(action.callUUID == id)
    }

    @Test func performStartReportsConnectingAndFulfills() {
        let controller = makeController()
        let action = SpyStartCallAction(call: id, handle: CXHandle(type: .generic, value: "Agent"))

        withProvider { controller.provider($0, perform: action) }

        #expect(reporter.reports == [
            .updated(id, callerName: "Agent", video: false, holding: false, dtmf: false),
            .startedConnecting(id),
        ])
        #expect(action.fulfilled)
        #expect(delegate.events.isEmpty)
    }

    @Test func performEndFulfillsAndForwards() {
        let controller = makeController()
        let action = SpyEndCallAction(call: id)

        withProvider { controller.provider($0, perform: action) }

        #expect(action.fulfilled)
        #expect(delegate.events == [.ended(id)])
        #expect(reporter.reports.isEmpty)
    }

    @Test func performMuteFulfillsAndForwards() {
        let controller = makeController()
        let mute = SpyMutedCallAction(call: id, muted: true)
        let unmute = SpyMutedCallAction(call: id, muted: false)

        withProvider { provider in
            controller.provider(provider, perform: mute)
            controller.provider(provider, perform: unmute)
        }

        #expect(mute.fulfilled)
        #expect(unmute.fulfilled)
        #expect(delegate.events == [.muted(true, id), .muted(false, id)])
    }

    @Test func audioActivationDeactivationAndResetAreForwarded() {
        let controller = makeController()

        withProvider { provider in
            controller.provider(provider, didActivate: AVAudioSession.sharedInstance())
            controller.provider(provider, didDeactivate: AVAudioSession.sharedInstance())
            controller.providerDidReset(provider)
        }

        #expect(delegate.events == [.activated, .deactivated, .reset])
    }

    @Test func reportsConnectedAndEachEndCause() {
        let controller = makeController()

        controller.reportConnected(id: id)
        controller.reportEnded(id: id, cause: .remoteEnded)
        controller.reportEnded(id: id, cause: .failed)
        controller.reportEnded(id: id, cause: .unanswered)

        #expect(reporter.reports == [
            .connected(id),
            .ended(id, .remoteEnded),
            .ended(id, .failed),
            .ended(id, .unanswered),
        ])
    }
}
