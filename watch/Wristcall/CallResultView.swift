import SwiftUI
import WristcallKit

/// What happened to a one-way call (decision W11): "Sending…" while the server transcribes and
/// delivers, then delivered, not delivered (with the reason) or nothing recorded. The transcript
/// shows whenever the server has one, also after a failed delivery; it goes below "Done" so a long
/// one never pushes the button off the screen.
struct CallResultView: View {
    let model: AppModel
    let result: CallResultModel

    var body: some View {
        ScrollView {
            VStack(spacing: 6) {
                Text(result.target.agent.displayName)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
                indicator
                    .font(.system(size: 32))
                    .frame(minHeight: 36)
                Text(headline)
                    .font(.headline)
                    .multilineTextAlignment(.center)
                if let detail {
                    Text(detail)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
                if case .timedOut = result.state {
                    Button("Check again") { result.checkAgain() }
                        .frame(minHeight: 44)
                }
                Button("Done") { model.dismissResult() }
                    .frame(minHeight: 44)
                if let text = transcript {
                    Text(text)
                        .font(.footnote)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(Color.gray.opacity(0.2), in: RoundedRectangle(cornerRadius: 8))
                }
            }
        }
    }

    @ViewBuilder
    private var indicator: some View {
        switch result.state {
        case .waiting:
            ProgressView()
        case .finished(let status):
            switch status.state {
            case .delivered:
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .failed:
                Image(systemName: "xmark.circle.fill").foregroundStyle(.red)
            default:
                Image(systemName: "mic.slash.fill").foregroundStyle(.secondary)
            }
        case .timedOut:
            Image(systemName: "clock.fill").foregroundStyle(.secondary)
        case .unavailable:
            Image(systemName: "questionmark.circle.fill").foregroundStyle(.secondary)
        }
    }

    private var headline: String {
        switch result.state {
        case .waiting: "Sending…"
        case .finished(let status):
            switch status.state {
            case .delivered: "Delivered"
            case .failed: "Not delivered"
            case .empty: "Nothing recorded."
            default: "Result unavailable."
            }
        case .timedOut: "Still processing. Check again later."
        case .unavailable: "Result unavailable."
        }
    }

    /// Why a delivery failed.
    private var detail: String? {
        guard case .finished(let status) = result.state, status.state == .failed else { return nil }
        switch status.failure {
        case .sttFailed?: return "Couldn't transcribe it."
        case .deliveryFailed?:
            let attempts = status.attempts.map { $0 == 1 ? " 1 attempt." : " \($0) attempts." } ?? ""
            return "The agent didn't confirm it." + attempts
        case .interrupted?: return "The server stopped while sending."
        case .internal?, .unknown?, nil: return "Server error."
        }
    }

    private var transcript: String? {
        guard case .finished(let status) = result.state,
              let text = status.text, !text.isEmpty
        else { return nil }
        return text
    }
}
