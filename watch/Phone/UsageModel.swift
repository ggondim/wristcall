import Observation
import WristcallKit

/// The month's call minutes of the Wristcall Cloud account (`usage` of `GET /v1/me` on the Cloud entry's node).
/// One instance in the environment: Agents and Settings ask it again when they appear. Without a Cloud entry it
/// does nothing (a self-hosted server has no usage).
@MainActor
@Observable
final class UsageModel {
    /// The last reading; kept when a later one fails.
    private(set) var usage: CloudUsage?
    /// The last request failed, or the node sent no usage this build can read.
    private(set) var unavailable = false

    @ObservationIgnored private let state: AppState

    init(state: AppState) {
        self.state = state
    }

    func load() async {
        guard let cloud = state.cloudServer else {
            // The account left the Cloud: nothing of the old reading stays.
            usage = nil
            unavailable = false
            return
        }
        do {
            let account = try await state.api(for: cloud).me()
            guard !Task.isCancelled else { return }
            usage = account.usage
            unavailable = account.usage == nil
        } catch {
            // The screen left: the request was cancelled, not failed.
            guard !Task.isCancelled else { return }
            unavailable = true
        }
    }
}
