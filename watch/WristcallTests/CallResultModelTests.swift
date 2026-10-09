import Foundation
import Testing
import WristcallKit
@testable import Wristcall

/// Decisions W10 and W11: the status of a one-way call, asked after hang-up until it is final.
@MainActor
struct CallResultModelTests {
    let pairing = StubPairingService()
    let clock = FakeClock()
    let credentials = Credentials(
        serverURL: URL(string: "https://agent.example.com")!, deviceId: "dev-1", token: "device-token", id: "srv-1")
    let target = AgentTarget(
        serverID: "srv-1", serverHost: "agent.example.com",
        agent: Agent(id: "ag_2", slug: "notes", displayName: "Notes", icon: "note.text", callType: .oneShot))
    let finishes = Recorder<Bool>()

    func status(_ state: CallState, failure: CallFailure? = nil, text: String? = nil, attempts: Int? = nil) -> CallStatus {
        CallStatus(id: "c_1", callType: .oneShot, state: state, failure: failure, text: text, attempts: attempts)
    }

    func makeResult() -> CallResultModel {
        let result = CallResultModel(
            target: target, callID: "c_1", credentials: credentials, pairing: pairing, poller: clock.poller)
        let finishes = finishes
        result.onFinished = { finishes.append($0) }
        return result
    }

    /// Starts and waits until nothing is running any more.
    func run(_ result: CallResultModel) async {
        result.start()
        await waitUntil { !result.isChecking }
    }

    @Test func deliveredFinishesAndReportsSuccess() async throws {
        let delivered = status(.delivered, text: "buy milk")
        pairing.callStatusResults = [.success(status(.processing)), .success(delivered)]
        let result = makeResult()

        #expect(result.state == .waiting(nil))
        await run(result)

        #expect(result.state == .finished(delivered))
        #expect(finishes.all == [true])
        #expect(pairing.calls == ["callStatus https://agent.example.com c_1", "callStatus https://agent.example.com c_1"])
        #expect(pairing.callStatusTokens == ["device-token", "device-token"])
    }

    @Test func failureFinishesWithTheReason() async throws {
        let failed = status(.failed, failure: .deliveryFailed, text: "buy milk", attempts: 3)
        pairing.callStatusResults = [.success(failed)]
        let result = makeResult()

        await run(result)

        #expect(result.state == .finished(failed))
        #expect(finishes.all == [false])
    }

    @Test func nothingRecordedFinishesWithoutSuccess() async throws {
        pairing.callStatusResults = [.success(status(.empty))]
        let result = makeResult()

        await run(result)

        #expect(result.state == .finished(status(.empty)))
        #expect(finishes.all == [false])
    }

    /// The network failing on the way does not stop the queries.
    @Test func networkErrorsKeepAsking() async throws {
        pairing.callStatusResults = [.failure(.network(.notConnectedToInternet)), .success(status(.delivered))]
        let result = makeResult()

        await run(result)

        #expect(result.state == .finished(status(.delivered)))
    }

    @Test func deadlineTimesOutWithTheLastStatus() async throws {
        // Then every fetch fails: the deadline passes with "processing" as the last word.
        pairing.callStatusResults = [.success(status(.processing))]
        let result = makeResult()

        await run(result)

        #expect(result.state == .timedOut(status(.processing)))
        #expect(pairing.callStatusCount > 100)
        #expect(finishes.all.isEmpty)
    }

    @Test func checkAgainStartsAnotherDeadline() async throws {
        let result = makeResult()
        await run(result)
        try #require(result.state == .timedOut(nil))
        let asked = pairing.callStatusCount
        pairing.callStatusResults = [.success(status(.processing)), .success(status(.delivered))]

        result.checkAgain()
        #expect(result.isChecking)
        await waitUntil { !result.isChecking }

        #expect(result.state == .finished(status(.delivered)))
        #expect(pairing.callStatusCount == asked + 2)
        #expect(finishes.all == [true])
    }

    @Test func comingBackToTheAppAsksAgainOnlyWithoutAFinalStatus() async throws {
        let result = makeResult()
        await run(result)
        try #require(result.state == .timedOut(nil))
        pairing.callStatusResults = [.success(status(.delivered))]

        result.appBecameActive()
        await waitUntil { !result.isChecking }
        #expect(result.state == .finished(status(.delivered)))
        let asked = pairing.callStatusCount

        result.appBecameActive()
        #expect(!result.isChecking)
        #expect(pairing.callStatusCount == asked)
    }

    @Test func comingBackWhileAskingDoesNotAskTwice() async throws {
        let gate = pairing.holdCallStatus()
        pairing.callStatusResults = [.success(status(.delivered))]
        let result = makeResult()
        result.start()
        await waitUntil { pairing.callStatusCount == 1 }

        result.appBecameActive()
        gate.open()
        await waitUntil { !result.isChecking }

        #expect(pairing.callStatusCount == 1)
        #expect(finishes.all == [true])
    }

    @Test func notFoundIsUnavailable() async throws {
        pairing.callStatusResults = [.failure(.notFound)]
        let result = makeResult()

        await run(result)
        result.appBecameActive()

        #expect(result.state == .unavailable)
        #expect(!result.isChecking)
        #expect(pairing.callStatusCount == 1)
        #expect(finishes.all.isEmpty)
    }

    @Test func unauthorizedIsUnavailableAndTellsTheModel() async throws {
        pairing.callStatusResults = [.failure(.unauthorized)]
        let result = makeResult()
        let unauthorized = Recorder<Bool>()
        result.onUnauthorized = { unauthorized.append(true) }

        await run(result)

        #expect(result.state == .unavailable)
        #expect(unauthorized.all == [true])
    }

    /// "Done" before the end: what the stopped query brings back later is ignored.
    @Test func stopIgnoresALateAnswer() async throws {
        let gate = pairing.holdCallStatus()
        pairing.callStatusResults = [.success(status(.delivered))]
        let result = makeResult()
        result.start()
        await waitUntil { pairing.callStatusCount == 1 }

        result.stop()
        #expect(!result.isChecking)
        gate.open()
        try await Task.sleep(for: .milliseconds(50))

        #expect(result.state == .waiting(nil))
        #expect(finishes.all.isEmpty)
    }
}
