import SwiftUI
import UIKit

/// The icon of an agent: its `icon` is a free text on the server, so a name the system does not
/// know shows a generic symbol (decision W8) instead of nothing.
enum AgentIcon {
    static let fallback = "waveform"

    /// `icon` when it names an SF Symbol this system has, else `waveform`.
    static func symbolName(for icon: String) -> String {
        UIImage(systemName: icon) == nil ? fallback : icon
    }
}

/// The symbol of `icon` at the size the grid and the call screens need.
struct AgentIconView: View {
    let icon: String
    var body: some View {
        Image(systemName: AgentIcon.symbolName(for: icon))
            .accessibilityHidden(true)
    }
}
