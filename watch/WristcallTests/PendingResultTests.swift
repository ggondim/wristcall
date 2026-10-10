import Foundation
import Testing
import WristcallKit
@testable import Wristcall

/// Decision W10, paid back: a one-way call's result survives quitting the app.
@MainActor
struct PendingResultTests {
    let store = InMemoryServerStore()
    let pairing = StubPairingService()
    let handler = StubCallHandler()
    let clock = FakeClock()
    let defaults = UserDefaults(suiteName: "PendingResultTests.\(UUID().uuidString)")!
    let server = URL(string: "https://agent.example.com")!
    let info = DeviceInfo(deviceId: "dev-1", deviceName: "Apple Watch", user: nil, agents: [
        Agent(id: "ag_1", slug: "default", displayName: "Agent"),
        Agent(id: "ag_2", slug: "notes", displayName: "Notes", icon: "note.text", callType: .oneShot),
    ])

    var pending: PendingResultStore { PendingResultStore(defaults: defaults) }

    func status(_ state: CallState) -> CallStatus {
        CallStatus(id: "c_1", callType: .oneShot, state: state, failure: nil, text: "buy milk", attempts: nil)
    }

    func entry(_ callID: String, server: String = "srv-1", age: TimeInterval = 60) -> PendingResult {
        PendingResult(callID: callID, serverID: server, agentID: "ag_2", startedAt: Date().addingTimeInterval(-age))
    }

    func makeModel() async throws -> AppModel {
        try store.save([Credentials(serverURL: server, deviceId: "dev-1", token: "t", id: "srv-1")])
        pairing.meResults = [.success(info)]
        let model = AppModel(
            pairing: pairing, store: store, defaults: defaults, sleep: { _ in }, resultPoller: clock.poller,
            pendingResults: pending)
        model.callHandler = handler
        return model
    }

    /// Home, then a one-way call that ends normally: the result screen is up.
    func openResult(_ model: AppModel, callID: String = "c_1") async throws -> CallResultModel {
        await model.launch()
        try #require(model.phase == .home)
        model.startCall(model.agents[1])
        model.callDidEnd(.normal, callID: callID)
        try #require(model.phase == .callResult)
        return try #require(model.callResult)
    }

    // MARK: - Recording and removing

    @Test func openingTheResultRecordsTheCall() async throws {
        let model = try await makeModel()
        pairing.callStatusResults = []
        _ = pairing.holdCallStatus()

        _ = try await openResult(model)

        let saved = try #require(pending.latest())
        #expect(saved.callID == "c_1")
        #expect(saved.serverID == "srv-1")
        #expect(saved.agentID == "ag_2")
        #expect(abs(saved.startedAt.timeIntervalSinceNow) < 60)
    }

    @Test func aFinalStatusRemovesTheEntry() async throws {
        let model = try await makeModel()
        pairing.callStatusResults = [.success(status(.delivered))]

        let result = try await openResult(model)
        await waitUntil { !result.isChecking }

        #expect(result.state == .finished(status(.delivered)))
        #expect(pending.latest() == nil)
    }

    @Test func aFailedDeliveryAlsoRemovesTheEntry() async throws {
        let model = try await makeModel()
        pairing.callStatusResults = [.success(status(.failed))]

        let result = try await openResult(model)
        await waitUntil { !result.isChecking }

        #expect(pending.latest() == nil)
    }

    @Test func unavailableRemovesTheEntry() async throws {
        let model = try await makeModel()
        pairing.callStatusResults = [.failure(.notFound)]

        let result = try await openResult(model)
        await waitUntil { !result.isChecking }

        #expect(result.state == .unavailable)
        #expect(pending.latest() == nil)
    }

    @Test func aTimeoutKeepsTheEntry() async throws {
        let model = try await makeModel()
        pairing.callStatusResults = []

        let result = try await openResult(model)
        await waitUntil { !result.isChecking }

        #expect(result.state == .timedOut(nil))
        #expect(pending.latest()?.callID == "c_1")
    }

    @Test func doneWhileWaitingKeepsTheEntry() async throws {
        let model = try await makeModel()
        _ = pairing.holdCallStatus()
        _ = try await openResult(model)

        model.dismissResult()

        #expect(model.phase == .home)
        #expect(pending.latest()?.callID == "c_1")
    }

    @Test func aConversationCallRecordsNothing() async throws {
        let model = try await makeModel()
        await model.launch()
        model.startCall(model.agents[0])
        model.callDidEnd(.normal)

        #expect(pending.latest() == nil)
    }

    // MARK: - Resuming

    @Test func launchReopensThePendingResult() async throws {
        pending.add(entry("c_9"))
        let model = try await makeModel()
        pairing.callStatusResults = [.success(status(.delivered))]

        await model.launch()

        #expect(model.phase == .callResult)
        let result = try #require(model.callResult)
        #expect(result.callID == "c_9")
        #expect(result.target.agent.id == "ag_2")
        #expect(result.target.agent.displayName == "Notes")
        #expect(result.target.serverID == "srv-1")
        await waitUntil { !result.isChecking }
        #expect(result.state == .finished(status(.delivered)))
        #expect(pending.latest() == nil)
        #expect(pairing.callStatusCount == 1)
    }

    @Test func resumingDoesNotExtendTheEntry() async throws {
        let old = entry("c_9", age: 3_600)
        pending.add(old)
        let model = try await makeModel()
        _ = pairing.holdCallStatus()

        await model.launch()

        #expect(model.phase == .callResult)
        #expect(pending.latest() == old)
    }

    @Test func anEntryOfAServerThatIsGoneIsDropped() async throws {
        pending.add(entry("c_9", server: "srv-gone"))
        let model = try await makeModel()

        await model.launch()

        #expect(model.phase == .home)
        #expect(model.callResult == nil)
        #expect(pending.latest() == nil)
        #expect(pairing.callStatusCount == 0)
    }

    @Test func aGoneServerDoesNotHideAnOlderEntryOfAPairedOne() async throws {
        pending.add(entry("c_old", age: 600))
        pending.add(entry("c_new", server: "srv-gone", age: 60))
        let model = try await makeModel()
        _ = pairing.holdCallStatus()

        await model.launch()

        #expect(model.callResult?.callID == "c_old")
    }

    @Test func anEntryOlderThanADayIsNotReopened() async throws {
        pending.add(entry("c_old", age: 90_000))
        let model = try await makeModel()

        await model.launch()

        #expect(model.phase == .home)
        #expect(pending.latest() == nil)
    }

    @Test func nothingPendingStaysHome() async throws {
        let model = try await makeModel()

        await model.launch()

        #expect(model.phase == .home)
        #expect(model.callResult == nil)
    }

    @Test func activationReopensThePendingResultAtHome() async throws {
        let model = try await makeModel()
        await model.launch()
        try #require(model.phase == .home)
        pending.add(entry("c_9"))
        _ = pairing.holdCallStatus()

        model.sceneDidBecomeActive()

        #expect(model.phase == .callResult)
        #expect(model.callResult?.callID == "c_9")
    }

    @Test func activationDuringACallLeavesItAlone() async throws {
        let model = try await makeModel()
        await model.launch()
        model.startCall(model.agents[1])
        try #require(model.phase == .inCall(model.agents[1]))
        pending.add(entry("c_9"))

        model.sceneDidBecomeActive()

        #expect(model.phase == .inCall(model.agents[1]))
        #expect(model.callResult == nil)
        #expect(pending.latest()?.callID == "c_9")
    }

    @Test func activationKeepsTheOpenResult() async throws {
        let model = try await makeModel()
        _ = pairing.holdCallStatus()
        let open = try await openResult(model, callID: "c_1")
        pending.add(entry("c_9"))

        model.sceneDidBecomeActive()

        #expect(model.callResult === open)
    }

    /// "Done" was the user's answer: raising the wrist again does not bring the screen back
    /// (the next launch does).
    @Test func activationDoesNotReopenWhatTheUserDismissed() async throws {
        let model = try await makeModel()
        _ = pairing.holdCallStatus()
        _ = try await openResult(model)
        model.dismissResult()

        model.sceneDidBecomeActive()

        #expect(model.phase == .home)
        #expect(model.callResult == nil)
        #expect(pending.latest()?.callID == "c_1")
    }

    @Test func aNewLaunchReopensWhatWasDismissedBefore() async throws {
        let model = try await makeModel()
        _ = pairing.holdCallStatus()
        _ = try await openResult(model)
        model.dismissResult()

        let next = try await makeModel()
        await next.launch()

        #expect(next.phase == .callResult)
        #expect(next.callResult?.callID == "c_1")
    }

    @Test func aMissingAgentStillOpensTheResult() async throws {
        pending.add(PendingResult(callID: "c_9", serverID: "srv-1", agentID: "ag_gone", startedAt: Date()))
        let model = try await makeModel()
        _ = pairing.holdCallStatus()

        await model.launch()

        #expect(model.phase == .callResult)
        #expect(model.callResult?.callID == "c_9")
        #expect(model.callResult?.target.serverID == "srv-1")
    }
}
