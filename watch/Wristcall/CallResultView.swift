import SwiftUI
import WristcallKit

/// The result of a one-way call. Temporary: the state as text and "Done", until the real screen.
struct CallResultView: View {
    let model: AppModel
    let result: CallResultModel

    var body: some View {
        VStack(spacing: 8) {
            Text(result.target.agent.displayName)
                .font(.headline)
            Text(stateText)
                .multilineTextAlignment(.center)
            Button("Done") { model.dismissResult() }
        }
    }

    private var stateText: String {
        switch result.state {
        case .waiting: "Sending…"
        case .finished(let status): status.state.wireValue
        case .timedOut: "Still processing. Check again later."
        case .unavailable: "Result unavailable."
        }
    }
}
