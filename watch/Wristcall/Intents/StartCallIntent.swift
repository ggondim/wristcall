import AppIntents
import WristcallKit

/// "Call agent": brings Wristcall to the foreground and calls the chosen agent, or the first one
/// (like the old shortcuts, complication and control) when none is chosen. The app does the
/// calling: this intent only leaves a `PendingCallStore` request, which `ShortcutCalls` acts on.
struct StartCallIntent: AppIntent {
    static let title: LocalizedStringResource = "Call agent"
    static let description: IntentDescription? = IntentDescription("Opens Wristcall and calls your agent.")
    /// CallKit and the microphone need the app itself, in the foreground (`openAppWhenRun` is
    /// deprecated since watchOS 26).
    static let supportedModes: IntentModes = .foreground(.immediate)

    /// Decision W14: optional, so the shortcuts made before agents existed keep calling the first one.
    @Parameter(title: "Agent")
    var agent: AgentEntity?

    init() {}

    init(agent: AgentEntity?) {
        self.agent = agent
    }

    @MainActor
    func perform() async throws -> some IntentResult {
        PendingCallStore().request(agent: agent?.id)
        return .result()
    }
}
