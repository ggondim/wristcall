import SwiftUI
import WristcallKit

/// The options of one agent, behind a long press on the grid: how the call ends the user's turn
/// (a conversation agent), or just "Call" (a one-way agent has no turn to end). One section per kind
/// of option, so later ones (other profiles, contacts) are new sections. Starting a call switches
/// `RootView` to the call screen, which drops this navigation stack; the call ends on Home. A call
/// that does not start (no connection) leaves this screen up, with the model's message below.
struct CallOptionsView: View {
    let model: AppModel
    let target: AgentTarget

    var body: some View {
        List {
            Section {
                if target.agent.callType.isOneWay {
                    Button {
                        // The agent's `turn_end` means nothing without an answer to wait for.
                        model.startCall(target)
                    } label: {
                        Label("Call", systemImage: "phone.fill")
                    }
                    .disabled(!model.canCall)
                } else {
                    ForEach(TurnEnd.allCases, id: \.self) { turnEnd in
                        Button {
                            // Explicit, even `auto`: the user picked it (decision W5).
                            model.startCall(target, turnEnd: turnEnd)
                        } label: {
                            VStack(alignment: .leading, spacing: 2) {
                                Label(turnEnd.title, systemImage: "phone.fill")
                                Text(turnEnd.subtitle)
                                    .font(.footnote)
                                    .foregroundStyle(.secondary)
                            }
                        }
                        .disabled(!model.canCall)
                    }
                }
            } header: {
                // `navigationSubtitle` does not exist on watchOS: the server is the section's header.
                Text(target.serverHost)
                    .lineLimit(1)
                    .minimumScaleFactor(0.8)
            } footer: {
                if let message = model.message {
                    Text(message)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle(target.agent.displayName)
    }
}

private extension TurnEnd {
    var title: String {
        switch self {
        case .auto: "Call (auto)"
        case .manual: "Call (manual)"
        }
    }

    var subtitle: String {
        switch self {
        case .auto: "Ends turn when you pause"
        case .manual: "Ends turn when you tap mute"
        }
    }
}
