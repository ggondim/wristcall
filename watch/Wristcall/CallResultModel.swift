import Foundation
import Observation
import os
import WristcallKit

/// What happened to a one-way call after hang-up (decisions W10 and W11): asks the server with
/// `GET /v1/calls/{id}` until the status is final, gives up after `CallStatusPoller.timeout` and
/// asks again when the app comes back to the foreground. The model lives in memory; the app keeps
/// the call id in a `PendingResultStore` until the status is final, so quitting the app does not
/// lose the result.
@Observable
@MainActor
final class CallResultModel {
    enum State: Equatable {
        /// Asking. The last status seen (`recording`, `processing`), if any came yet.
        case waiting(CallStatus?)
        /// `delivered`, `failed` (with `failure`) or `empty`.
        case finished(CallStatus)
        /// The deadline passed without a final status; "Check again" starts another one.
        case timedOut(CallStatus?)
        /// `404` (the call left the history) or `401` (the server removed this watch).
        case unavailable
    }

    let target: AgentTarget
    let callID: String
    private(set) var state: State = .waiting(nil)
    /// Told once per final status, `true` for `delivered`. The app plays a haptic with it.
    var onFinished: ((Bool) -> Void)?
    /// `401`: the token of this server is no longer valid. The app model removes the server.
    var onUnauthorized: (() -> Void)?
    /// Told once when there is nothing more to ask: a final status, or no result at all (`404`,
    /// `401`). Not on a timeout: the call may still finish. The app forgets the pending result.
    var onSettled: (() -> Void)?

    /// A query is running.
    var isChecking: Bool { task != nil }

    private let credentials: Credentials
    private let pairing: any PairingService
    private let poller: CallStatusPoller
    private var task: Task<Void, Never>?
    /// Bumped on every start and stop, so a stopped query that answers late changes nothing.
    private var run = 0
    private static let log = Logger(subsystem: "io.github.ggondim.wristcall", category: "result")

    init(
        target: AgentTarget,
        callID: String,
        credentials: Credentials,
        pairing: any PairingService,
        poller: CallStatusPoller = CallStatusPoller()
    ) {
        self.target = target
        self.callID = callID
        self.credentials = credentials
        self.pairing = pairing
        self.poller = poller
    }

    /// Starts asking, unless a query is already running.
    func start() {
        guard task == nil else { return }
        run += 1
        let run = run
        state = .waiting(lastStatus)
        let pairing = pairing
        let server = credentials.serverURL
        let token = credentials.token
        let callID = callID
        let poller = poller
        task = Task { [weak self] in
            let outcome = await poller.run(
                fetch: { try await pairing.callStatus(server: server, token: token, callID: callID) },
                onUpdate: { [weak self] status in await self?.received(status, run: run) })
            self?.finish(outcome, run: run)
        }
    }

    /// "Check again" after the deadline: another full deadline from now.
    func checkAgain() {
        guard !isOver else { return }
        stop()
        start()
    }

    /// The app is in the foreground again. A query that lived through the background may be
    /// overdue, about to fail on a radio that is still waking up, or never have started: without a
    /// final status, drop whatever is running and ask afresh, with a new deadline.
    func appBecameActive() {
        guard !isOver else { return }
        stop()
        start()
    }

    /// A push says the call has a final status: drop whatever is running and ask once now. The
    /// first `GET` already brings the final status, so nothing waits for the next poll.
    func pushArrived() {
        guard !isOver else { return }
        stop()
        start()
    }

    /// "Done", or the screen went away.
    func stop() {
        run += 1
        task?.cancel()
        task = nil
    }

    // MARK: - Private

    /// Nothing more to ask: a final status, or no result at all.
    var isOver: Bool {
        switch state {
        case .finished, .unavailable: true
        case .waiting, .timedOut: false
        }
    }

    private var lastStatus: CallStatus? {
        switch state {
        case .waiting(let status), .timedOut(let status): status
        case .finished(let status): status
        case .unavailable: nil
        }
    }

    private func received(_ status: CallStatus, run: Int) {
        guard run == self.run, !status.state.isFinal else { return }
        state = .waiting(status)
    }

    private func finish(_ outcome: CallStatusPoller.Outcome, run: Int) {
        guard run == self.run else { return }
        task = nil
        switch outcome {
        case .final(let status):
            // The id is the user's call, the text is the user's speech: neither goes to the log.
            Self.log.notice("call result: \(status.state.wireValue, privacy: .public)")
            state = .finished(status)
            onFinished?(status.state == .delivered)
            onSettled?()
        case .timedOut(let last):
            Self.log.notice("call result: still \(last?.state.wireValue ?? "unknown", privacy: .public) at the deadline")
            state = .timedOut(last ?? lastStatus)
        case .notFound:
            Self.log.notice("call result: not found")
            state = .unavailable
            onSettled?()
        case .unauthorized:
            Self.log.notice("call result: unauthorized")
            state = .unavailable
            onUnauthorized?()
            onSettled?()
        case .cancelled:
            break
        }
    }
}
