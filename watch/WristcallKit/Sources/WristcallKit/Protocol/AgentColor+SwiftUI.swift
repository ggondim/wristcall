import SwiftUI

extension AgentColor {
    /// The system color of the same name.
    public var swiftUI: Color {
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

    /// A missing color wears `AgentColor.fallback`.
    public static func swiftUI(_ color: AgentColor?) -> Color { resolved(color).swiftUI }
}
