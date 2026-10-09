import Foundation
import Testing
import WristcallKit
@testable import Wristcall

@MainActor
struct AppModelTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let defaults = UserDefaults(suiteName: "AppModelTests.\(UUID().uuidString)")!
    let server = URL(string: "https://agent.example.com")!
    let device = PairedDevice(deviceId: "dev-1", token: "device-token")
    let assistant = Agent(id: "ag_1", slug: "default", displayName: "Agent")
    let notes = Agent(id: "ag_2", slug: "notes", displayName: "Notes", icon: "note.text", callType: .oneShot)
    let info = DeviceInfo(
        deviceId: "dev-1", deviceName: "Apple Watch", user: UserInfo(id: "usr_1", handle: "gustavo"),
        agents: [
            Agent(id: "ag_1", slug: "default", displayName: "Agent"),
            Agent(id: "ag_2", slug: "notes", displayName: "Notes", icon: "note.text", callType: .oneShot),
        ])
    /// The first server, as stored.
    let first = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token", id: "srv-1")
    /// A second server, of another user.
    let second = Credentials(
        serverURL: URL(string: "https://home.example.com")!, deviceId: "dev-9", token: "home-token", id: "srv-2")
    let house = Agent(id: "ag_9", slug: "house", displayName: "House")
    let homeInfo = DeviceInfo(
        deviceId: "dev-9", deviceName: "Apple Watch", user: UserInfo(id: "usr_9", handle: "family"),
        agents: [Agent(id: "ag_9", slug: "house", displayName: "House")])
    let pending = PairingRequest(requestId: "4821", pollToken: "poll-secret", expiresAt: .now.addingTimeInterval(600))

    /// What pairing should have stored: the server and device above, under the local id the model gave it.
    func expectedCredentials() throws -> Credentials {
        Credentials(serverURL: server, device: device, id: try #require(try store.load().first).id)
    }

    func target(_ agent: Agent, on credentials: Credentials) -> AgentTarget {
        AgentTarget(serverID: credentials.id, serverHost: credentials.serverURL.host()!, agent: agent)
    }

    func makeModel(
        reachability: (any NetworkReachability)? = nil,
        savedCatalog: [CatalogAgent] = [],
        sleep: @escaping PairingClient.Sleep = { _ in }
    ) -> AppModel {
        AppModel(
            pairing: pairing, store: store, defaults: defaults, reachability: reachability, sleep: sleep,
            savedCatalog: savedCatalog)
    }

    func makePairedModel(reachability: (any NetworkReachability)? = nil) async throws -> AppModel {
        try store.save([first])
        pairing.meResults = [.success(info)]
        let model = makeModel(reachability: reachability)
        await model.launch()
        try #require(model.phase == .home)
        try #require(model.agents.count == 2)
        return model
    }

    /// `first` with `info` and `second` with `homeInfo`, both answered.
    func makeModelWithTwoServers(reachability: (any NetworkReachability)? = nil) async throws -> AppModel {
        try store.save([first, second])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let model = makeModel(reachability: reachability)
        await model.launch()
        try #require(model.agents.count == 3)
        return model
    }

    // MARK: - Launch

    @Test func launchWithoutCredentialsShowsPairing() async {
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(!model.hasServers)
        #expect(pairing.calls.isEmpty)
    }

    @Test func launchWithCredentialsLoadsTheAgents() async throws {
        let model = try await makePairedModel()
        #expect(model.servers == [ServerEntry(credentials: first, status: .ready(info))])
        #expect(model.agents == [target(assistant, on: first), target(notes, on: first)])
        #expect(model.agents.first?.ref == AgentRef(serverID: "srv-1", agentID: "ag_1"))
        #expect(model.agents.first?.id == "srv-1/ag_1")
        #expect(model.servers.first?.host == "agent.example.com")
        #expect(model.hasServers)
        #expect(!model.isLoadingServers)
        #expect(model.canCall)
        #expect(pairing.calls == ["me https://agent.example.com"])
    }

    /// Review Focus 1: a watch paired on 0.1.0 opens on the grid, without pairing again. The old
    /// Keychain item becomes the list, under a local id that stays the same on the next launch.
    @Test func launchMigratesTheSingleServerOf010() async throws {
        let service = "io.github.ggondim.wristcall.tests.\(UUID().uuidString)"
        let keychain = KeychainServerStore(service: service)
        let legacy = KeychainCredentialStore(service: service, account: "default")
        defer {
            try? keychain.deleteAll()
            try? legacy.delete()
        }
        try legacy.save(Credentials(serverURL: server, device: device))
        pairing.meResults = [.success(info), .success(info)]

        let model = AppModel(pairing: pairing, store: keychain, defaults: defaults, sleep: { _ in })
        await model.launch()

        #expect(model.phase == .home)
        #expect(model.agents.map(\.agent) == [assistant, notes])
        #expect(try legacy.load() == nil)
        let migrated = try #require(try keychain.load().first)
        #expect(migrated.serverURL == server)
        #expect(migrated.token == "device-token")
        #expect(model.agents.first?.serverID == migrated.id)

        let relaunched = AppModel(pairing: pairing, store: keychain, defaults: defaults, sleep: { _ in })
        await relaunched.launch()
        #expect(relaunched.agents.first?.serverID == migrated.id)
    }

    @Test func twoServersShowTheirAgentsTogetherInServerOrder() async throws {
        try store.save([first, second])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let slowFirst = pairing.holdMe(for: first.serverURL)
        let model = makeModel()

        let launch = Task { await model.launch() }
        // Each server shows up as soon as it answers, even before the one above it.
        await waitUntil { model.servers.count == 2 && model.servers[1].status == .ready(homeInfo) }
        #expect(model.phase == .home)
        #expect(model.servers[0].status == .loading)
        #expect(model.isLoadingServers)
        #expect(model.agents == [target(house, on: second)])
        #expect(model.canCall)

        slowFirst.open()
        await launch.value

        #expect(!model.isLoadingServers)
        #expect(model.agents == [target(assistant, on: first), target(notes, on: first), target(house, on: second)])
    }

    /// Review Focus 2.
    @Test func oneUnreachableServerKeepsTheOthersCallable() async throws {
        try store.save([first, second])
        pairing.setMeResults([.failure(.network(.timedOut)), .success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let handler = StubCallHandler()
        let model = makeModel()
        model.callHandler = handler

        await model.launch()

        #expect(model.phase == .home)
        #expect(model.servers.map(\.status) == [.unavailable(AppModel.Message.unreachable), .ready(homeInfo)])
        #expect(model.agents == [target(house, on: second)])
        #expect(model.canCall)
        #expect(try store.load() == [first, second])

        model.startCall(target(house, on: second))
        #expect(handler.started.map(\.credentials) == [second])
        model.callDidEnd(.normal)

        await model.retry(serverID: first.id)
        #expect(model.agents.count == 3)
    }

    /// Review Focus 2: a revoked server goes away by itself; the others stay.
    @Test func unauthorizedServerIsRemovedAlone() async throws {
        try store.save([first, second])
        pairing.setMeResults([.failure(.unauthorized)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let model = makeModel()

        await model.launch()

        #expect(model.phase == .home)
        #expect(model.servers.map(\.credentials) == [second])
        #expect(try store.load() == [second])
        #expect(model.message == "agent.example.com: this watch was removed on the server.")
        #expect(model.agents == [target(house, on: second)])
    }

    @Test func unauthorizedOnTheOnlyServerGoesToPairing() async throws {
        try store.save([first])
        pairing.meResults = [.failure(.unauthorized)]
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == "agent.example.com: this watch was removed on the server.")
        #expect(!model.hasServers)
        #expect(try store.load().isEmpty)
    }

    @Test func unreachableServerAtLaunchKeepsTheCredentialsAndRetries() async throws {
        try store.save([first])
        pairing.meResults = [.failure(.network(.notConnectedToInternet)), .success(info)]
        let model = makeModel()
        await model.launch()
        #expect(model.phase == .home)
        #expect(model.servers.first?.status == .unavailable(AppModel.Message.unreachable))
        #expect(!model.canCall)
        #expect(try store.load() == [first])

        await model.retry()
        #expect(model.servers.first?.status == .ready(info))
        #expect(model.canCall)
        #expect(model.message == nil)
    }

    @Test func corruptedKeychainItemIsDeletedAndGoesToPairing() async {
        let broken = ThrowingServerStore(.corruptedData)
        let model = AppModel(pairing: pairing, store: broken, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unpaired)
        #expect(model.message == nil)
        #expect(broken.deleteCount == 1)
        #expect(pairing.calls.isEmpty)
    }

    @Test func lockedKeychainKeepsTheItemAndOffersRetry() async {
        let locked = ThrowingServerStore(.keychain(errSecInteractionNotAllowed))
        let model = AppModel(pairing: pairing, store: locked, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unavailable)
        #expect(model.message == AppModel.Message.locked)
        #expect(locked.deleteCount == 0)
        #expect(pairing.calls.isEmpty)
    }

    @Test func otherKeychainErrorsKeepTheItemAndShowAMessage() async {
        let failing = ThrowingServerStore(.keychain(errSecNotAvailable))
        let model = AppModel(pairing: pairing, store: failing, defaults: defaults)
        await model.launch()
        #expect(model.phase == .unavailable)
        #expect(model.message == AppModel.Message.keychain)
        #expect(failing.deleteCount == 0)
        #expect(pairing.calls.isEmpty)
    }

    // MARK: - Answers that arrive late

    /// A server removed while its `/v1/me` is in flight stays removed when the answer arrives.
    @Test func removedServerIsNotBroughtBackByALateAnswer() async throws {
        try store.save([first, second])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let slowFirst = pairing.holdMe(for: first.serverURL)
        let model = makeModel()
        let launch = Task { await model.launch() }
        await waitUntil { model.servers.count == 2 && model.servers[1].status == .ready(homeInfo) }

        await model.removeServer(id: first.id)
        slowFirst.open()
        await launch.value

        #expect(model.servers.map(\.credentials) == [second])
        #expect(model.agents == [target(house, on: second)])
        #expect(try store.load() == [second])
    }

    /// "Retry" tapped again while the first retry is loading does not ask the server twice.
    @Test func retryWhileLoadingDoesNotLoadTwice() async throws {
        try store.save([first])
        pairing.meResults = [.failure(.network(.timedOut)), .success(info), .success(info)]
        let model = makeModel()
        await model.launch()
        let slow = pairing.holdMe(for: first.serverURL)

        let retry = Task { await model.retry(serverID: first.id) }
        await waitUntil { pairing.calls.count == 2 }
        #expect(model.servers.first?.status == .loading)
        await model.retry(serverID: first.id)
        await model.retry()
        slow.open()
        await retry.value

        #expect(pairing.calls == ["me https://agent.example.com", "me https://agent.example.com"])
        #expect(model.servers.first?.status == .ready(info))
    }

    // MARK: - Flow A: code resolved by the directory

    @Test func codeFlowResolvesPairsAndLoadsTheAgents() async throws {
        pairing.resolveResults = [.success(server)]
        pairing.pairResults = [.success(.paired(device))]
        pairing.meResults = [.success(info)]
        let model = makeModel()
        await model.launch()

        await model.pair(code: PairingCode("1234 5678")!).value

        #expect(model.phase == .home)
        #expect(model.agents.map(\.agent) == [assistant, notes])
        #expect(model.message == nil)
        #expect(try store.load() == [expectedCredentials()])
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
        #expect(try store.load().isEmpty)
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

        #expect(model.phase == .home)
        #expect(model.agents.map(\.agent) == [assistant, notes])
        #expect(model.customServerURL == nil)
        #expect(try store.load().map(\.serverURL) == [local])
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
        #expect(model.phase == .home)
        #expect(model.agents.map(\.agent) == [assistant, notes])
        #expect(try store.load() == [expectedCredentials()])
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
        #expect(try store.load().isEmpty)
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

    // MARK: - Adding a server

    @Test func pairingAddsAServerWithoutRemovingTheOthers() async throws {
        let model = try await makePairedModel()
        model.addServer()
        #expect(model.phase == .unpaired)
        #expect(model.isAddingServer)
        #expect(model.useServerURL(second.serverURL.absoluteString))
        pairing.pairResults = [.success(.paired(PairedDevice(deviceId: "dev-9", token: "home-token")))]
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .home)
        #expect(!model.isAddingServer)
        #expect(model.servers.map(\.credentials.serverURL) == [first.serverURL, second.serverURL])
        #expect(try store.load().map(\.serverURL) == [first.serverURL, second.serverURL])
        #expect(model.agents.map(\.agent) == [assistant, notes, house])
        #expect(pairing.unpairTokens.isEmpty)
    }

    /// Decision W2: pairing the same server and user again keeps the local id (complications
    /// still point to it) and revokes the old token without waiting for it.
    @Test func pairingTheSameServerAndUserAgainReplacesItAndRevokesTheOldToken() async throws {
        let model = try await makePairedModel()
        model.addServer()
        #expect(model.useServerURL(server.absoluteString))
        pairing.pairResults = [.success(.paired(PairedDevice(deviceId: "dev-2", token: "new-token")))]
        pairing.meResults = [.success(info)]

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .home)
        let replaced = Credentials(serverURL: server, deviceId: "dev-2", token: "new-token", id: "srv-1")
        #expect(model.servers == [ServerEntry(credentials: replaced, status: .ready(info))])
        #expect(try store.load() == [replaced])
        await waitUntil { pairing.unpairTokens == ["device-token"] }
        #expect(pairing.unpairTokens == ["device-token"])
    }

    @Test func pairingTheSameServerAsAnotherUserAddsAnEntry() async throws {
        let model = try await makePairedModel()
        model.addServer()
        #expect(model.useServerURL(server.absoluteString))
        pairing.pairResults = [.success(.paired(PairedDevice(deviceId: "dev-2", token: "new-token")))]
        let other = DeviceInfo(
            deviceId: "dev-2", deviceName: "Apple Watch", user: UserInfo(id: "usr_2", handle: "ana"), agents: [house])
        pairing.meResults = [.success(other)]

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.servers.count == 2)
        #expect(model.servers.map(\.credentials.token) == ["device-token", "new-token"])
        #expect(try store.load().count == 2)
        #expect(pairing.unpairTokens.isEmpty)
    }

    /// The device already exists on the server: a `/v1/me` that fails right after pairing keeps
    /// the new server, with "Retry", instead of losing its token.
    @Test func pairedServerThatDoesNotAnswerIsKeptToRetry() async throws {
        pairing.resolveResults = [.success(server)]
        pairing.pairResults = [.success(.paired(device))]
        pairing.meResults = [.failure(.network(.timedOut))]
        let model = makeModel()
        await model.launch()

        await model.pair(code: PairingCode("12345678")!).value

        #expect(model.phase == .home)
        #expect(try store.load() == [expectedCredentials()])
        #expect(model.servers.first?.status == .unavailable(AppModel.Message.unreachable))
    }

    @Test func cancelAddServerGoesBackHome() async throws {
        let model = try await makePairedModel()
        model.addServer()
        model.cancelAddServer()
        #expect(model.phase == .home)
        #expect(!model.isAddingServer)
        #expect(model.agents.count == 2)
    }

    // MARK: - Removing a server

    @Test func removeServerRevokesOnTheServerAndForgetsIt() async throws {
        let model = try await makeModelWithTwoServers()
        await model.removeServer(id: first.id)
        #expect(model.phase == .home)
        #expect(model.message == nil)
        #expect(model.servers.map(\.credentials) == [second])
        #expect(try store.load() == [second])
        #expect(pairing.calls.last == "unpair https://agent.example.com")
        #expect(pairing.unpairTokens == ["device-token"])
        #expect(model.agents == [target(house, on: second)])
    }

    @Test func removeServerOfflineStillForgetsItLocally() async throws {
        let model = try await makeModelWithTwoServers()
        pairing.unpairResult = .failure(.network(.notConnectedToInternet))
        await model.removeServer(id: first.id)
        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.unpairedOffline)
        #expect(try store.load() == [second])
    }

    @Test func removeServerAlreadyRevokedCountsAsDone() async throws {
        let model = try await makePairedModel()
        pairing.unpairResult = .failure(.unauthorized)
        await model.removeServer(id: first.id)
        #expect(model.message == nil)
        #expect(try store.load().isEmpty)
    }

    @Test func removingTheLastServerGoesToPairing() async throws {
        let model = try await makePairedModel()
        await model.removeServer(id: first.id)
        #expect(model.phase == .unpaired)
        #expect(!model.hasServers)
        #expect(model.agents.isEmpty)
        #expect(try store.load().isEmpty)
    }

    // MARK: - Agent catalog

    @Test func onAgentsChangedReceivesTheCatalog() async throws {
        try store.save([first, second])
        pairing.setMeResults([.success(info)], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let model = makeModel()
        var catalogs: [[CatalogAgent]] = []
        model.onAgentsChanged = { catalogs.append($0) }

        await model.launch()

        let assistantEntry = CatalogAgent(
            ref: AgentRef(serverID: "srv-1", agentID: "ag_1"), slug: "default", displayName: "Agent",
            icon: "waveform", callType: "conversation", serverHost: "agent.example.com")
        let notesEntry = CatalogAgent(
            ref: AgentRef(serverID: "srv-1", agentID: "ag_2"), slug: "notes", displayName: "Notes",
            icon: "note.text", callType: "one-shot", serverHost: "agent.example.com")
        let houseEntry = CatalogAgent(
            ref: AgentRef(serverID: "srv-2", agentID: "ag_9"), slug: "house", displayName: "House",
            icon: "waveform", callType: "conversation", serverHost: "home.example.com")
        #expect(catalogs.last == [assistantEntry, notesEntry, houseEntry])
        #expect(model.catalog == [assistantEntry, notesEntry, houseEntry])

        await model.removeServer(id: second.id)
        #expect(catalogs.last == [assistantEntry, notesEntry])

        // Nothing changed: nobody is told again.
        let count = catalogs.count
        await model.retry()
        #expect(catalogs.count == count)
    }

    /// A server that is down keeps the agents the last catalog had for it, so complications and
    /// shortcuts that point to them survive a launch without network.
    @Test func catalogKeepsTheAgentsOfAnUnreachableServer() async throws {
        try store.save([first, second])
        pairing.setMeResults([.failure(.network(.notConnectedToInternet))], for: first.serverURL)
        pairing.setMeResults([.success(homeInfo)], for: second.serverURL)
        let saved = CatalogAgent(
            ref: AgentRef(serverID: "srv-1", agentID: "ag_1"), slug: "default", displayName: "Agent",
            icon: "waveform", callType: "conversation", serverHost: "agent.example.com")
        let forgotten = CatalogAgent(
            ref: AgentRef(serverID: "srv-gone", agentID: "ag_5"), slug: "old", displayName: "Old",
            icon: "waveform", callType: "conversation", serverHost: "old.example.com")
        let model = makeModel(savedCatalog: [forgotten, saved])
        var catalogs: [[CatalogAgent]] = []
        model.onAgentsChanged = { catalogs.append($0) }

        await model.launch()

        #expect(catalogs.last?.map(\.id) == ["srv-1/ag_1", "srv-2/ag_9"])
        #expect(model.agents == [target(house, on: second)])
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

    // MARK: - Call

    @Test func startCallSendsTheAgentAndNoTurnEnd() async throws {
        let model = try await makePairedModel()
        let handler = StubCallHandler()
        model.callHandler = handler
        let chosen = target(notes, on: first)

        model.startCall(chosen)

        #expect(model.phase == .inCall(chosen))
        #expect(model.callActivity == .connecting)
        #expect(handler.started == [CallRequest(credentials: first, target: chosen, turnEnd: nil)])
    }

    /// "…" (call options) sends the mode the user picked, even `auto`.
    @Test(arguments: TurnEnd.allCases)
    func startCallFromTheOptionsSendsTheTurnEnd(turnEnd: TurnEnd) async throws {
        let model = try await makePairedModel()
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(target(assistant, on: first), turnEnd: turnEnd)

        #expect(handler.started.map(\.turnEnd) == [turnEnd])
    }

    @Test func startCallHandsTheRequestToTheHandlerAndEndsThroughIt() async throws {
        let model = try await makePairedModel()
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall()

        let chosen = target(assistant, on: first)
        #expect(model.phase == .inCall(chosen))
        #expect(handler.started == [CallRequest(credentials: first, target: chosen, turnEnd: nil)])

        model.endCall()
        #expect(handler.endRequests == 1)
        #expect(model.phase == .inCall(chosen))

        model.callDidEnd(.connectionLost)
        #expect(model.phase == .home)
        #expect(model.message == "Connection lost")
    }

    @Test func startCallWithoutAnAgentCallsTheFirstCallableOne() async throws {
        let video = Agent(id: "ag_0", slug: "video", displayName: "Video", callType: .unknown("video"))
        try store.save([first])
        pairing.meResults = [.success(DeviceInfo(
            deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [video, assistant]))]
        let handler = StubCallHandler()
        let model = makeModel()
        model.callHandler = handler
        await model.launch()

        model.startCall(agent: nil)

        #expect(handler.started.map(\.target) == [target(assistant, on: first)])
    }

    @Test func startCallWithAnAgentRefCallsThatAgent() async throws {
        let model = try await makeModelWithTwoServers()
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(agent: "srv-2/ag_9")

        #expect(handler.started.map(\.target) == [target(house, on: second)])
        #expect(handler.started.map(\.credentials) == [second])
    }

    /// Review Focus 4 / decision W4: an agent that is gone never turns into a call to another one.
    @Test(arguments: ["srv-1/ag_404", "srv-9/ag_1", "not a ref", "srv-1/ag_1/x", ""])
    func startCallWithUnknownAgentShowsAMessage(ref: String) async throws {
        let model = try await makePairedModel()
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(agent: ref)

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == "Agent not found.")
    }

    @Test func startCallToAnAgentOfAnUnreachableServerSaysSo() async throws {
        try store.save([first])
        pairing.meResults = [.failure(.network(.timedOut))]
        let handler = StubCallHandler()
        let model = makeModel()
        model.callHandler = handler
        await model.launch()

        model.startCall(agent: "srv-1/ag_1")

        #expect(handler.started.isEmpty)
        #expect(model.message == "Can't reach agent.example.com.")
    }

    /// Decision W7: a call type this build does not know is shown, never called.
    @Test func unknownCallTypeDoesNotCall() async throws {
        let video = Agent(id: "ag_0", slug: "video", displayName: "Video", callType: .unknown("video"))
        try store.save([first])
        pairing.meResults = [.success(DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [video]))]
        let handler = StubCallHandler()
        let model = makeModel()
        model.callHandler = handler
        await model.launch()
        #expect(!model.canCall)

        model.startCall(target(video, on: first))
        model.startCall(agent: "srv-1/ag_0")

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == "Update Wristcall to call this agent.")
    }

    /// Without a network path no CallKit call starts (its "Call Failed" alert crashes the
    /// watch's system UI): Home stays, with a message.
    @Test(arguments: TurnEnd.allCases)
    func noNetworkPathKeepsHomeAndShowsNoConnection(turnEnd: TurnEnd) async throws {
        let network = FakeNetworkReachability(false)
        let model = try await makePairedModel(reachability: network)
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(target(assistant, on: first), turnEnd: turnEnd)

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == "No connection")
        #expect(model.canCall)

        network.hasNetworkPath = true
        model.startCall(target(assistant, on: first), turnEnd: turnEnd)

        #expect(handler.started.map(\.turnEnd) == [turnEnd])
        #expect(model.message == nil)
    }

    @Test(arguments: TurnEnd.allCases)
    func satisfiedNetworkPathStartsTheCall(turnEnd: TurnEnd) async throws {
        let model = try await makePairedModel(reachability: FakeNetworkReachability(true))
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(target(assistant, on: first), turnEnd: turnEnd)

        #expect(handler.started.map(\.turnEnd) == [turnEnd])
        #expect(model.phase == .inCall(target(assistant, on: first)))
    }

    /// A monitor that has not reported yet does not block the call.
    @Test(arguments: TurnEnd.allCases)
    func unknownNetworkPathStartsTheCall(turnEnd: TurnEnd) async throws {
        let model = try await makePairedModel(reachability: FakeNetworkReachability(nil))
        let handler = StubCallHandler()
        model.callHandler = handler

        model.startCall(target(assistant, on: first), turnEnd: turnEnd)

        #expect(handler.started.map(\.turnEnd) == [turnEnd])
        #expect(model.phase == .inCall(target(assistant, on: first)))
    }

    /// A 4401 on a call removes the server of that call, not the others.
    @Test func callEndedAsUnauthorizedRemovesOnlyThatServer() async throws {
        let model = try await makeModelWithTwoServers()
        model.callHandler = StubCallHandler()
        model.startCall(target(house, on: second))

        model.callDidEnd(.unauthorized)

        #expect(model.phase == .home)
        #expect(model.message == "home.example.com: this watch was removed on the server.")
        #expect(model.servers.map(\.credentials) == [first])
        #expect(try store.load() == [first])
    }

    @Test func callEndedAsUnauthorizedOnTheLastServerGoesBackToPairing() async throws {
        let model = try await makePairedModel()
        model.callHandler = StubCallHandler()
        model.startCall()

        model.callDidEnd(.unauthorized)

        #expect(model.phase == .unpaired)
        #expect(model.message == "agent.example.com: this watch was removed on the server.")
        #expect(try store.load().isEmpty)
    }

    @Test func callDidFailGoesHomeWithTheMessage() async throws {
        let model = try await makePairedModel()
        model.callHandler = StubCallHandler()
        model.startCall()

        model.callDidFail(message: AppModel.Message.microphoneUnavailable)

        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.microphoneUnavailable)
    }

    @Test func withoutHandlerEndCallReturnsHome() async throws {
        let model = try await makePairedModel()
        model.startCall()
        model.endCall()
        #expect(model.phase == .home)
        #expect(model.message == nil)
    }
}
