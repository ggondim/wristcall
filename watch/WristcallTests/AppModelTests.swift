import Foundation
import Testing
import WristcallKit
@testable import Wristcall

@MainActor
struct AppModelTests {
    let store = InMemoryCredentialStore()
    let pairing = StubPairingService()
    let defaults = UserDefaults(suiteName: "AppModelTests.\(UUID().uuidString)")!
    let server = URL(string: "https://agent.example.com")!
    let device = PairedDevice(deviceId: "dev-1", token: "device-token")
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", profiles: [
        Profile(name: "default", displayName: "Agent"),
        Profile(name: "demo", displayName: "Demo"),
    ])
    let pending = PairingRequest(requestId: "4821", pollToken: "poll-secret", expiresAt: .now.addingTimeInterval(600))

    func makeModel(sleep: @escaping PairingClient.Sleep = { _ in }) -> AppModel {
        AppModel(pairing: pairing, store: store, defaults: defaults, sleep: sleep)
    }

    func makePairedModel() async throws -> AppModel {
        try store.save(Credentials(serverURL: server, device: device))
        pairing.meResults = [.success(info)]
        let model = makeModel()
        await model.launch()
        try #require(model.phase == .ready(info))
        return model
    }

    // MARK: - Launch

    @Test func launchWithoutCredentialsShowsPairing() async {
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(pairing.calls.isEmpty)
    }

    @Test func launchWithCredentialsLoadsTheProfiles() async throws {
        let model = try await makePairedModel()
        #expect(model.profile == Profile(name: "default", displayName: "Agent"))
        #expect(model.serverURL == server)
        #expect(model.canCall)
        #expect(pairing.calls == ["me https://agent.example.com"])
    }

    @Test func unauthorizedAtLaunchClearsTheCredentials() async throws {
        try store.save(Credentials(serverURL: server, device: device))
        pairing.meResults = [.failure(.unauthorized)]
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == AppModel.Message.revoked)
        #expect(model.serverURL == nil)
        #expect(try store.load() == nil)
    }

    @Test func unreachableServerAtLaunchKeepsTheCredentialsAndRetries() async throws {
        try store.save(Credentials(serverURL: server, device: device))
        pairing.meResults = [.failure(.network(.notConnectedToInternet)), .success(info)]
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unavailable)
        #expect(model.message == AppModel.Message.unreachable)
        #expect(!model.canCall)
        #expect(try store.load() != nil)

        await model.retry()
        #expect(model.phase == .ready(info))
        #expect(model.message == nil)
    }

    @Test func corruptedKeychainItemIsDeletedAndGoesToPairing() async {
        let broken = ThrowingCredentialStore(.corruptedData)
        let model = AppModel(pairing: pairing, store: broken, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(broken.deleteCount == 1)
        #expect(pairing.calls.isEmpty)
    }

    @Test func lockedKeychainKeepsTheItemAndOffersRetry() async {
        let locked = ThrowingCredentialStore(.keychain(errSecInteractionNotAllowed))
        let model = AppModel(pairing: pairing, store: locked, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unavailable)
        #expect(model.message == AppModel.Message.locked)
        #expect(locked.deleteCount == 0)
        #expect(pairing.calls.isEmpty)
    }

    @Test func otherKeychainErrorsKeepTheItemAndShowAMessage() async {
        let failing = ThrowingCredentialStore(.keychain(errSecNotAvailable))
        let model = AppModel(pairing: pairing, store: failing, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unavailable)
        #expect(model.message == AppModel.Message.keychain)
        #expect(failing.deleteCount == 0)
        #expect(pairing.calls.isEmpty)
    }

    // MARK: - Flow A: code resolved by the directory

    @Test func codeFlowResolvesPairsAndLoadsTheProfiles() async throws {
        pairing.resolveResults = [.success(server)]
        pairing.pairResults = [.success(.paired(device))]
        pairing.meResults = [.success(info)]
        let model = makeModel()
        await model.launch()

        await model.pair(code: PairingCode("1234 5678")!).value

        #expect(model.phase == .ready(info))
        #expect(model.message == nil)
        #expect(try store.load() == Credentials(serverURL: server, device: device))
        #expect(pairing.calls == [
            "resolve 12345678 https://wristcall-pair.trigram.com.br",
            "pair https://agent.example.com code=12345678 name=Apple Watch",
            "me https://agent.example.com",
        ])
    }

    @Test func invalidCodeShowsAMessageAndStaysUnpaired() async throws {
        pairing.resolveResults = [.success(server)]
        pairing.pairResults = [.failure(.invalidCode)]
        let model = makeModel()
        await model.launch()

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .unpaired)
        #expect(model.message == "Invalid or expired code.")
        #expect(!model.isBusy)
        #expect(try store.load() == nil)
    }

    /// `PairingClient.resolve` retries a 404 three times before throwing `codeNotFound`
    /// (covered by its own tests in task 3); the model only shows the result.
    @Test func codeUnknownToTheDirectoryShowsAMessage() async throws {
        pairing.resolveResults = [.failure(.codeNotFound)]
        let model = makeModel()
        await model.launch()

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .unpaired)
        #expect(model.message == "Code not found. Check it or use the server URL.")
        #expect(pairing.calls == ["resolve 12345678 https://wristcall-pair.trigram.com.br"])
    }

    @Test func pairingUsesTheDirectoryFromSettings() async throws {
        pairing.resolveResults = [.failure(.network(.cannotFindHost))]
        let model = makeModel()
        #expect(model.setDirectory("https://pair.example.org"))
        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.message == AppModel.Message.directoryUnreachable)
        #expect(pairing.calls == ["resolve 12345678 https://pair.example.org"])
    }

    // MARK: - Flow A': server URL, then code

    @Test func serverURLFlowSkipsTheDirectory() async throws {
        let local = URL(string: "http://127.0.0.1:8765")!
        pairing.pairResults = [.success(.paired(device))]
        pairing.meResults = [.success(info)]
        let model = makeModel()
        await model.launch()

        #expect(!model.useServerURL("http://agent.example.com"))
        #expect(model.message == AppModel.Message.invalidURL)
        #expect(model.customServerURL == nil)

        #expect(model.useServerURL("http://127.0.0.1:8765"))
        #expect(model.message == nil)
        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .ready(info))
        #expect(model.customServerURL == nil)
        #expect(try store.load()?.serverURL == local)
        #expect(pairing.calls == [
            "pair http://127.0.0.1:8765 code=12345678 name=Apple Watch",
            "me http://127.0.0.1:8765",
        ])
    }

    // MARK: - Flow B: server URL without code, owner approves

    @Test func approvalFlowPollsEveryTwoSecondsUntilPaired() async throws {
        pairing.pairResults = [.success(.pending(pending))]
        pairing.pollResults = [.success(.pending(requestId: "4821")), .success(.paired(device))]
        pairing.meResults = [.success(info)]
        let sleeps = Recorder<Duration>()
        let phases = Recorder<AppPhase>()
        let box = ModelBox()
        let model = makeModel(sleep: { duration in
            sleeps.append(duration)
            await MainActor.run { phases.append(box.model!.phase) }
        })
        box.model = model
        await model.launch()
        #expect(model.useServerURL(server.absoluteString))

        await model.requestApproval().value

        #expect(sleeps.all == [.seconds(2), .seconds(2)])
        #expect(phases.all == [.pairing(requestId: "4821"), .pairing(requestId: "4821")])
        #expect(model.phase == .ready(info))
        #expect(try store.load() == Credentials(serverURL: server, device: device))
        #expect(pairing.pollTokens == ["poll-secret", "poll-secret"])
        #expect(pairing.calls == [
            "pair https://agent.example.com code=nil name=Apple Watch",
            "poll https://agent.example.com",
            "poll https://agent.example.com",
            "me https://agent.example.com",
        ])
        // The poll token is a client secret: nothing the screens read may contain it.
        #expect(!String(describing: phases.all).contains("poll-secret"))
        #expect(!String(describing: model.phase).contains("poll-secret"))
    }

    @Test func approvalFlowEndsWhenTheRequestIsGone() async throws {
        pairing.pairResults = [.success(.pending(pending))]
        pairing.pollResults = [.success(.gone)]
        let model = makeModel()
        await model.launch()
        #expect(model.useServerURL(server.absoluteString))

        await model.requestApproval().value

        #expect(model.phase == .unpaired)
        #expect(model.message == "The request expired. Try again.")
        #expect(try store.load() == nil)
    }

    @Test func cancelStopsPolling() async throws {
        pairing.pairResults = [.success(.pending(pending))]
        let model = makeModel(sleep: { _ in try await Task.sleep(for: .seconds(30)) })
        await model.launch()
        #expect(model.useServerURL(server.absoluteString))

        let task = model.requestApproval()
        await waitUntil { model.phase == .pairing(requestId: "4821") }
        #expect(model.phase == .pairing(requestId: "4821"))
        model.cancelPairing()
        await task.value

        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(!model.isBusy)
        #expect(pairing.pollTokens.isEmpty)
    }

    @Test func cancelledRequestsNeverShowAMessage() async throws {
        let model = makeModel()
        await model.launch()

        pairing.resolveResults = [.failure(.network(.cancelled))]
        await model.pair(code: PairingCode("12345678")!).value
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)

        let bare = makeModel(sleep: { _ in throw CancellationError() })
        await bare.launch()
        #expect(bare.useServerURL(server.absoluteString))
        pairing.pairResults = [.success(.pending(pending))]
        await bare.requestApproval().value
        #expect(bare.phase == .unpaired)
        #expect(bare.message == nil)
    }

    @Test func requestApprovalNeedsAServerURL() async {
        let model = makeModel()
        await model.launch()
        await model.requestApproval().value
        #expect(model.phase == .unpaired)
        #expect(model.message == AppModel.Message.serverURLNeeded)
        #expect(pairing.calls.isEmpty)
    }

    // MARK: - Unpair

    @Test func unpairRevokesOnTheServerAndClearsTheKeychain() async throws {
        let model = try await makePairedModel()
        await model.unpair()
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(try store.load() == nil)
        #expect(pairing.calls.last == "unpair https://agent.example.com")
    }

    @Test func unpairOfflineStillClearsLocally() async throws {
        let model = try await makePairedModel()
        pairing.unpairResult = .failure(.network(.notConnectedToInternet))
        await model.unpair()
        #expect(model.phase == .unpaired)
        #expect(model.message == AppModel.Message.unpairedOffline)
        #expect(model.serverURL == nil)
        #expect(try store.load() == nil)
    }

    // MARK: - Settings

    @Test func directoryIsValidatedAndPersisted() {
        let model = makeModel()
        #expect(model.directoryURL == PairingClient.defaultDirectory)
        #expect(!model.setDirectory("http://pair.example.org"))
        #expect(model.directoryURL == PairingClient.defaultDirectory)
        #expect(model.setDirectory("pair.example.org"))
        #expect(makeModel().directoryURL == URL(string: "https://pair.example.org")!)

        model.resetDirectory()
        #expect(model.directoryURL == PairingClient.defaultDirectory)
        #expect(makeModel().directoryURL == PairingClient.defaultDirectory)
    }

    // MARK: - Call hook (filled by tasks 8 to 10)

    @Test func startCallHandsCredentialsAndFirstProfileToTheHandler() async throws {
        let model = try await makePairedModel()
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall()

        let agent = Profile(name: "default", displayName: "Agent")
        #expect(model.phase == .inCall(agent))
        #expect(handler.started == [CallRequest(credentials: Credentials(serverURL: server, device: device), profile: agent)])
        model.endCall()
        #expect(handler.endRequests == 1)
        #expect(model.phase == .inCall(agent))

        model.callDidEnd(.connectionLost)
        #expect(model.phase == .ready(info))
        #expect(model.message == "Connection lost")
    }

    @Test func callEndedAsUnauthorizedGoesBackToPairing() async throws {
        let model = try await makePairedModel()
        model.callHandler = StubCallHandler()
        model.startCall()

        model.callDidEnd(.unauthorized)

        #expect(model.phase == .unpaired)
        #expect(model.message == AppModel.Message.revoked)
        #expect(try store.load() == nil)
    }

    @Test func withoutHandlerEndCallReturnsHome() async throws {
        let model = try await makePairedModel()
        model.startCall()
        model.endCall()
        #expect(model.phase == .ready(info))
        #expect(model.message == nil)
    }
}
