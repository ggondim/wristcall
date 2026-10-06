import AppIntents
import SwiftUI
import WidgetKit

/// A Control Center button (watchOS 26) that runs `StartCallIntent`. The intent is compiled
/// into the app too and asks for `.foreground(.immediate)`, so the system runs it in the app,
/// where `ShortcutCalls` picks up the request. On Apple Watch Ultra the same control can be
/// assigned to the Action Button (supported, not tested: the maintainer's Series 7 has none).
struct CallControl: ControlWidget {
    static let kind = "io.github.ggondim.wristcall.call-control"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: StartCallIntent()) {
                Label("Call agent", systemImage: "phone.fill")
            }
        }
        .displayName("Call agent")
        .description("Calls your agent with Wristcall.")
    }
}
