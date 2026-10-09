import Foundation
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
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [
        Agent(id: "ag_1", slug: "default", displayName: "Agent"),
        Agent(id: "ag_2", slug: "notes", displayName: "Notes", callType: .oneShot),
    ])
    let server = URL(string: "https://agent.example.com")!

    var pending: PendingCallStore { PendingCallStore(defaults: defaults, notificationCenter: center) }

    func makeModel(paired: Bool) throws -> AppModel {
        if paired {
            try store.save([Credentials(serverURL: server, deviceId: "dev-1", token: "t", id: "srv-1")])
            pairing.meResults = [.success(info)]
        }
        let model = AppModel(pairing: pairing, store: store, defaults: defaults, sleep: { _ in })
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

    @Test func pendingRequestForAnAgentThatIsGoneDoesNotCall() async throws {
        let model = try makeModel(paired: true)
        await model.launch()
        pending.request(agent: "srv-1/ag_404")

        await ShortcutCalls(store: pending, model: model).check()

        #expect(handler.started.isEmpty)
        #expect(model.phase == .home)
        #expect(model.message == AppModel.Message.agentNotFound)
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

    @Test func theIntentRunsInTheAppInTheForeground() {
        // `perform()` itself is not run here: it writes to the standard store, which the test
        // host app watches, and a paired host would place a real call.
        #expect(StartCallIntent.supportedModes == .foreground(.immediate))
        #expect(WristcallShortcuts.appShortcuts.count == 1)
    }
}
