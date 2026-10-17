import SwiftUI
import WristcallKit

extension AgentColor {
    /// The system color of the same name (a missing color wears `AgentColor.fallback`).
    var swiftUI: Color {
        switch self {
        case .red: .red
        case .orange: .orange
        case .yellow: .yellow
        case .green: .green
        case .mint: .mint
        case .teal: .teal
        case .blue: .blue
        case .indigo: .indigo
        case .purple: .purple
        case .pink: .pink
        }
    }

    static func swiftUI(_ color: AgentColor?) -> Color { resolved(color).swiftUI }
}

/// An agent's mark: a circle in its color with the icon in white.
struct AgentBadge: View {
    let icon: String
    let color: AgentColor?
    var size: CGFloat = 40

    var body: some View {
        Image(systemName: UIImage(systemName: icon) == nil ? "questionmark" : icon)
            .font(.system(size: size * 0.5, weight: .medium))
            .foregroundStyle(.white)
            .frame(width: size, height: size)
            .background(AgentColor.swiftUI(color), in: .circle)
            .accessibilityHidden(true)
    }
}
