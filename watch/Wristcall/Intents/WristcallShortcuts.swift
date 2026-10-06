import AppIntents

/// Shortcuts the system offers without setup (Shortcuts app; Siri only with a paid account).
struct WristcallShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartCallIntent(),
            phrases: [
                "Call agent with \(.applicationName)",
                "Call my agent with \(.applicationName)",
                "Start a \(.applicationName) call",
            ],
            shortTitle: "Call agent",
            systemImageName: "phone.fill"
        )
    }
}
