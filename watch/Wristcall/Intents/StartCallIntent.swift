import AppIntents
import WristcallKit

/// "Call agent": brings Wristcall to the foreground and calls the agent (the default profile,
/// like the Call button). The app does the calling: this intent only leaves a
/// `PendingCallStore` request, which `ShortcutCalls` acts on.
struct StartCallIntent: AppIntent {
    static let title: LocalizedStringResource = "Call agent"
    static let description: IntentDescription? = IntentDescription("Opens Wristcall and calls your agent.")
    /// CallKit and the microphone need the app itself, in the foreground (`openAppWhenRun` is
    /// deprecated since watchOS 26).
    static let supportedModes: IntentModes = .foreground(.immediate)

    @MainActor
    func perform() async throws -> some IntentResult {
        PendingCallStore().request()
        return .result()
    }
}
