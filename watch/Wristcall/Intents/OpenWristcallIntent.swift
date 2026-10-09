import AppIntents
import WristcallKit

/// The action of the agent control (`AgentCallControl`): calls the configured agent or, with none
/// configured, only brings Wristcall to the foreground (decision W20). `StartCallIntent` cannot do
/// the latter: without an agent it calls the first one, as the controls and shortcuts of 0.1.0 expect.
/// A single type for both cases because a control's template has no `if` to pick between intents.
struct OpenWristcallIntent: AppIntent {
    static let title: LocalizedStringResource = "Open Wristcall"
    static let description: IntentDescription? = IntentDescription("Opens Wristcall and calls the agent of the control.")
    /// Runs in the app, like `StartCallIntent`: a call needs CallKit and the microphone.
    static let supportedModes: IntentModes = .foreground(.immediate)
    /// Only the control runs it; Shortcuts already has "Call agent".
    static let isDiscoverable = false

    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}

    init(agent: AgentEntity?) {
        self.agent = agent
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        if let agent {
            PendingCallStore().request(agent: agent.id)
        }
        return .result()
    }
}
