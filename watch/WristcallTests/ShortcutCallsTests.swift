import Foundation
import Intents
import Testing
import WristcallKit
@testable import Wristcall

@MainActor
struct ShortcutCallsTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let handler = StubCallHandler()
    let defaults = UserDefaults(suiteName: "ShortcutCallsTests.\(UUID().uuidString)")!
    let center = NotificationCenter()
    let clock = FakeClock()
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [
        Agent(id: "ag_1", slug: "default", displayName: "Agent"),
        Agent(id: "ag_2", slug: "notes", displayName: "Notes", callType: .oneShot),
    ])
    let server = URL(string: "https://agent.example.com")!
    let home = URL(string: "https://home.example.com")!
    let homeInfo = DeviceInfo(deviceId: "dev-9", deviceName: "Apple Watch", user: nil, agents: [
        Agent(id: "ag_9", slug: "house", displayName: "House"),
        Agent(id: "ag_8", slug: "notes", displayName: "Notes", callType: .monologue),
    ])

    var pending: PendingCallStore { PendingCallStore(defaults: defaults, notificationCenter: center) }

    func makeModel(paired: Bool) throws -> AppModel {
        if paired {
            try store.save([Credentials(serverURL: server, deviceId: "dev-1", token: "t", id: "srv-1")])
            pairing.meResults = [.success(info)]
        }
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller)
        model.callHandler = handler
        return model
    }

    /// `server` (srv-1, answers `first`, else `info`) and `home` (srv-2, answers `homeInfo`).
    func makeModelWithTwoServers(first: Result<DeviceInfo, PairingError>? = nil) throws -> AppModel {
        try store.save([
            Credentials(serverURL: server, deviceId: "dev-1", token: "t", id: "srv-1"),
            Credentials(serverURL: home, deviceId: "dev-9", token: "h", id: "srv-2"),
        ])
        pairing.setMeResults([first ?? .success(info)], for: server)
        pairing.setMeResults([.success(homeInfo)], for: home)
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller)
        model.callHandler = handler
        return model
    }

    @Test func pendingRequestStartsTheCallWhenReady() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        pending.request()

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.count == 1)
        #expect(model.phase == .inCall(model.agents[0]))
        #expect(!pending.isPending)
    }

    @Test func pendingRequestWithAnAgentCallsThatAgent() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        pending.request(agent: "srv-1/ag_2")

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.map(\.target.agent.slug) == ["notes"])
        #expect(!pending.isPending)
    }

    /// Review Focus 4: a complication, control or shortcut that points to an agent that was deleted,
    /// to a server that was removed, or to nothing valid never calls another agent.
    @Test(arguments: ["srv-1/ag_404", "srv-gone/ag_1", "not-a-ref"])
    func unknownAgentDoesNotCall(ref: String) async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        pending.request(agent: ref)

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.agentNotFound)
        #expect(!pending.isPending)
    }

    /// Review Focus 4 / decision W20, the whole widget path: the configured agent is gone, the query
    /// keeps its id, the complication's link carries it, and the app says so instead of calling.
    @Test func aComplicationOfAnAgentThatIsGoneDoesNotCall() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        let catalog = AgentCatalog(defaults: defaults)
        catalog.save(model.agents.map(\.catalogEntry))
        let configured = try await AgentQuery(catalog: catalog).entities(for: ["srv-1/ag_404"])

        #expect(ShortcutCalls.request(from: AgentEntity.link(for: configured.first), store: pending))
        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.agentNotFound)
    }

    /// Decision W20: a new complication with no agent chosen only opens the app.
    @Test func theOpenLinkAsksForNoCall() async throws {
        #expect(!ShortcutCalls.request(from: AgentEntity.link(for: nil), store: pending))
        #expect(!pending.isPending)
    }

    /// Review Focus 2 / decision W17: a request waits only for the server it needs.
    @Test func requestForAReadyServerDoesNotWaitForAnotherLoading() async throws {
        let model = try makeModelWithTwoServers()
        let slow = pairing.holdMe(for: home)
        let launch = Task { await model.launch() }
        await waitUntil { model.servers.first?.isReady == true }
        pending.request(agent: "srv-1/ag_2")
        let started = ContinuousClock.now

        await ShortcutCalls(store: pending, model: model, launchWait: .seconds(10)).check()

        #expect(ContinuousClock.now - started < .seconds(2))
        #expect(model.isLoadingServers)
        #expect(handler.started.map(\.target.id) == ["srv-1/ag_2"])
        slow.open()
        await launch.value
    }

    /// Decision W17: "the first agent" is the first server's; with that server down nothing is called.
    @Test func firstAgentNeverComesFromTheSecondServerWhileTheFirstIsDown() async throws {
        let model = try makeModelWithTwoServers(first: .failure(.network(.notConnectedToInternet)))
        await model.launch()
        try #require(model.agents.map(\.id) == ["srv-2/ag_9", "srv-2/ag_8"])
        pending.request()

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == "Can't reach agent.example.com.")
        #expect(!pending.isPending)
    }

    /// The first server still loading when the wait runs out counts as down, too.
    @Test func firstAgentWaitsOnlyForTheFirstServerAndThenGivesUp() async throws {
        let model = try makeModelWithTwoServers()
        let slow = pairing.holdMe(for: server)
        let launch = Task { await model.launch() }
        await waitUntil { model.servers.last?.isReady == true }
        pending.request()

        await ShortcutCalls(store: pending, model: model, launchWait: .milliseconds(100)).check()

        #expect(handler.started.isEmpty)
        #expect(model.message == "Can't reach agent.example.com.")
        slow.open()
        await launch.value
    }

    // MARK: - The system's redial (decision W19)

    @Test func redialCallsTheOnlyAgentWithThatName() async throws {
        let model = try makeModelWithTwoServers()
        await model.launch()
        pending.request(agentNamed: "House")

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.map(\.target.id) == ["srv-2/ag_9"])
    }

    /// Two agents named "Notes", or none named "Nobody": never a guess.
    @Test(arguments: ["Notes", "Nobody", "house"])
    func redialOfADuplicateOrUnknownNameDoesNotCall(name: String) async throws {
        let model = try makeModelWithTwoServers()
        await model.launch()
        pending.request(agentNamed: name)

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.agentNotFound)
        #expect(!pending.isPending)
    }

    /// The CallKit handle is the agent's name; an intent without one redials like the old complication.
    @Test func redialRecordsTheHandleOfTheFirstContact() throws {
        func intent(_ handles: [String?]) -> INStartCallIntent {
            INStartCallIntent(
                callRecordFilter: nil, callRecordToCallBack: nil, audioRoute: .unknown, destinationType: .normal,
                contacts: handles.map { handle in
                    INPerson(
                        personHandle: INPersonHandle(value: handle, type: .unknown), nameComponents: nil,
                        displayName: nil, image: nil, contactIdentifier: nil, customIdentifier: nil)
                },
                callCapability: .audioCall)
        }

        ShortcutCalls.requestRedial(of: intent(["Notes", "House"]), store: pending)
        #expect(pending.consume() == PendingCall(agentName: "Notes"))

        ShortcutCalls.requestRedial(of: intent([" "]), store: pending)
        #expect(pending.consume() == PendingCall())

        ShortcutCalls.requestRedial(of: intent([nil]), store: pending)
        #expect(pending.consume() == PendingCall())

        ShortcutCalls.requestRedial(of: nil, store: pending)
        #expect(pending.consume() == PendingCall())
    }

    /// The result screen of a one-way call does not swallow the request: it closes and the call starts.
    @Test func requestWhileTheResultIsOpenClosesItAndCalls() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        _ = pairing.holdCallStatus()
        model.startCall(model.agents[1])
        model.callDidEnd(.normal, callID: "c_1")
        try #require(model.phase == .callResult)
        let result = try #require(model.callResult)
        pending.request(agent: "srv-1/ag_1")

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.map(\.target.agent.slug) == ["notes", "default"])
        #expect(model.phase == .inCall(model.agents[0]))
        #expect(model.callResult == nil)
        #expect(!result.isChecking)
        #expect(!pending.isPending)
    }

    @Test func callLinkWithAnAgentRecordsThatAgent() {
        #expect(ShortcutCalls.request(from: ShortcutLink.call(agent: "srv-1/ag_2"), store: pending))
        #expect(pending.consume() == PendingCall(agent: "srv-1/ag_2"))

        #expect(ShortcutCalls.request(from: URL(string: "wristcall://call")!, store: pending))
        #expect(pending.consume() == PendingCall(agent: nil))

        #expect(!ShortcutCalls.request(from: URL(string: "wristcall://settings?agent=srv-1/ag_2")!, store: pending))
        #expect(!pending.isPending)
    }

    @Test func requestDuringLaunchWaitsForTheAgents() async throws {
        let model = try makeModel(paired: true)
        #expect(model.phase == .launching)
        pending.request()
        let calls = ShortcutCalls(store: pending, model: model)

        let check = Task { await calls.check() }
        try await Task.sleep(for: .milliseconds(100))
        #expect(handler.started.isEmpty)
        await model.launch()
        await check.value

        #expect(handler.started.count == 1)
    }

    @Test func withoutARequestNothingHappens() async throws {
        let model = try makeModel(paired: true)
        await model.launch()

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
    }

    @Test func unpairedWatchDropsTheRequest() async throws {
        let model = try makeModel(paired: false)
        await model.launch()
        pending.request()

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .unpaired)
        #expect(!pending.isPending)
    }

    /// Launch reads the Keychain and goes Home at once; the agents arrive with each `/v1/me`.
    @Test func requestWaitsForTheServersStillLoading() async throws {
        let model = try makeModel(paired: true)
        let slow = pairing.holdMe(for: server)
        let launch = Task { await model.launch() }
        await waitUntil { model.phase == .home }
        #expect(model.isLoadingServers)
        pending.request(agent: "srv-1/ag_2")

        let check = Task { await ShortcutCalls(store: pending, model: model).check() }
        try await Task.sleep(for: .milliseconds(100))
        #expect(handler.started.isEmpty)
        slow.open()
        await launch.value
        await check.value

        #expect(handler.started.map(\.target.agent.slug) == ["notes"])
    }

    @Test func launchThatNeverEndsGivesUpAndDropsTheRequest() async throws {
        let model = try makeModel(paired: true)  // never launched: stays .launching
        pending.request()

        await ShortcutCalls(store: pending, model: model, launchWait: .milliseconds(100)).check()

        #expect(handler.started.isEmpty)
        #expect(!pending.isPending)
    }

    @Test func aSecondCheckDuringACallDoesNotStartAnother() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        let calls = ShortcutCalls(store: pending, model: model)
        pending.request()
        await calls.check()

        pending.request()
        await calls.check()

        #expect(handler.started.count == 1)
        #expect(!pending.isPending)
    }

    /// Decision W20: the agent control with no agent opens the app and asks for no call.
    @Test func theOpenIntentWithoutAnAgentAsksForNoCall() async throws {
        #expect(OpenWristcallIntent.supportedModes == .foreground(.immediate))
        #expect(OpenWristcallIntent().agent == nil)
        let standard = PendingCallStore()
        let wasPending = standard.isPending

        _ = try await OpenWristcallIntent().perform()

        #expect(standard.isPending == wasPending)
    }

    @Test func theIntentRunsInTheAppInTheForeground() {
        // `perform()` itself is not run here: it writes to the standard store, which the test
        // host app watches, and a paired host would place a real call.
        #expect(StartCallIntent.supportedModes == .foreground(.immediate))
        #expect(WristcallShortcuts.appShortcuts.count == 1)
    }

    /// The control and the "Call <agent>" shortcut carry the agent; the old ones carry none.
    @Test func theIntentCarriesTheAgentItWasMadeFor() {
        let notes = AgentEntity(CatalogAgent(
            ref: AgentRef(serverID: "srv-1", agentID: "ag_2"), slug: "notes", displayName: "Notes",
            icon: "note.text", callType: "one-shot", serverHost: "agent.example.com"))
        #expect(StartCallIntent(agent: notes).agent?.id == "srv-1/ag_2")
        #expect(StartCallIntent().agent == nil)
    }
}
