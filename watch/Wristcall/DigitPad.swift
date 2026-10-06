import SwiftUI

/// Our own 3x4 keypad (the system number pad is not available on watchOS).
struct DigitPad: View {
    @Binding var model: DigitPadModel
    var isEnabled = true

    var body: some View {
        Grid(horizontalSpacing: 4, verticalSpacing: 4) {
            ForEach(DigitPadModel.layout, id: \.self) { row in
                GridRow {
                    ForEach(row, id: \.self) { key in
                        Button {
                            model.press(key)
                        } label: {
                            label(for: key)
                                .font(.title3.monospacedDigit())
                                .frame(maxWidth: .infinity, minHeight: 34)
                                .background(Color.gray.opacity(0.3), in: RoundedRectangle(cornerRadius: 8))
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel(accessibilityLabel(for: key))
                    }
                }
            }
        }
        .disabled(!isEnabled)
    }

    @ViewBuilder
    private func label(for key: DigitPadModel.Key) -> some View {
        switch key {
        case .digit(let value): Text("\(value)")
        case .delete: Image(systemName: "delete.left")
        case .clear: Text("C")
        }
    }

    private func accessibilityLabel(for key: DigitPadModel.Key) -> String {
        switch key {
        case .digit(let value): "\(value)"
        case .delete: "Delete"
        case .clear: "Clear"
        }
    }
}

#Preview {
    @Previewable @State var pad = DigitPadModel()
    DigitPad(model: $pad)
}
