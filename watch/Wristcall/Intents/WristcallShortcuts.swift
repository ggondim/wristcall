import AppIntents

/// Shortcuts the system offers without setup (Shortcuts app; Siri only with a paid account). The
/// app calls `updateAppShortcutParameters()` when its agents change, so "Call <agent>" lists them.
struct WristcallShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartCallIntent(),
            phrases: [
                "Call agent with \(.applicationName)",
                "Call my agent with \(.applicationName)",
                "Start a \(.applicationName) call",
                "Call \(\.$agent) with \(.applicationName)",
            ],
            shortTitle: "Call agent",
            systemImageName: "phone.fill"
        )
    }
}
