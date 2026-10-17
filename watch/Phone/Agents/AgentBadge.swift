import SwiftUI
import WristcallKit

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
