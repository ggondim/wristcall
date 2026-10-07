import SwiftUI
import WristcallKit

/// Agent options ("…" on Home): ways to call the agent. Each kind of option is its own section,
/// so new ones (other profiles, for example) are new sections.
///
/// Starting a call moves `AppModel.phase` to `.inCall`: `RootView` drops Home and its
/// `NavigationStack`, so this screen is gone and the call ends on a fresh Home.
/// Agent options behind "…" on Home: how the call ends the user's turn. One section per kind of
/// option, so later ones (other profiles, contacts) are new sections. Starting a call switches
/// `RootView` to the call screen, which drops this navigation stack; the call ends on Home.
struct CallOptionsView: View {
    let model: AppModel

    var body: some View {
        List {
            Section {
                ForEach(TurnEnd.allCases, id: \.self) { turnEnd in
                    Button {
                        model.startCall(turnEnd: turnEnd)
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
        }
        .navigationTitle(model.profile?.displayName ?? "wristcall")
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
