import SwiftUI

/// Grid of SF Symbols for an agent, with a preview of the pick.
struct IconPicker: View {
    /// Names the server accepts (`^[a-z0-9]+(\.[a-z0-9]+)*$`).
    static let symbols = [
        "waveform", "brain", "sparkles", "bubble.left", "text.bubble",
        "envelope", "calendar", "checklist", "note.text", "lightbulb",
        "house", "car", "briefcase", "cart", "heart",
        "book", "globe", "terminal", "wrench", "phone",
    ]

    @Binding var selection: String

    private var choices: [String] {
        Self.symbols.contains(selection) ? Self.symbols : Self.symbols + [selection]
    }

    var body: some View {
        VStack(spacing: 12) {
            AgentIcon(name: selection, size: 56)
                .accessibilityHidden(true)
            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 8), count: 5), spacing: 8) {
                ForEach(choices, id: \.self) { name in
                    Button {
                        selection = name
                    } label: {
                        Image(systemName: name)
                            .font(.title3)
                            .frame(maxWidth: .infinity, minHeight: 44)
                            .background(
                                RoundedRectangle(cornerRadius: 10)
                                    .fill(name == selection ? Color.accentColor.opacity(0.2) : Color.clear)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 10)
                                    .stroke(name == selection ? Color.accentColor : Color.secondary.opacity(0.3), lineWidth: 1)
                            )
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(name)
                    .accessibilityAddTraits(name == selection ? .isSelected : [])
                }
            }
        }
        .padding(.vertical, 4)
    }
}

/// An agent's symbol on a rounded tile (a name the system does not know shows a generic symbol).
struct AgentIcon: View {
    let name: String
    var size: CGFloat = 36

    var body: some View {
        Image(systemName: UIImage(systemName: name) == nil ? "questionmark.circle" : name)
            .font(.system(size: size * 0.5))
            .frame(width: size, height: size)
            .foregroundStyle(Color.accentColor)
            .background(RoundedRectangle(cornerRadius: size * 0.25).fill(Color.accentColor.opacity(0.15)))
    }
}
