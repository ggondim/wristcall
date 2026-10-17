import SwiftUI
import WristcallKit

/// How far one line of the setup screen has got.
enum SetupStepState: Equatable {
    case pending, active, done
}

/// The first screen of a watch with no server: what the automatic setup is doing, "Open wristcall on your
/// iPhone" when it has not worked out in a few seconds (or is paused), "No agents yet" once it has. Pairing
/// with a code lives in Settings > Advanced.
struct SetupView: View {
    let model: AppModel
    let setup: SetupModel
    @Environment(AccountLoginModel.self) private var login

    /// The two lines of the progress screen for a phase.
    static func steps(for phase: SetupModel.Phase) -> (signIn: SetupStepState, agents: SetupStepState) {
        switch phase {
        case .idle, .signingIn: (.active, .pending)
        case .gettingAgents: (.done, .active)
        case .waitingForPhone: (.pending, .pending)
        case .noAgents, .ready: (.done, .done)
        }
    }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 8) {
                    content
                    if let message = setup.message {
                        Text(message)
                            .font(.footnote)
                            .foregroundStyle(.red)
                            .multilineTextAlignment(.center)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            .navigationTitle(title)
            .toolbarForegroundStyle(.mint, for: .navigationBar)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    NavigationLink {
                        SettingsView(model: model)
                    } label: {
                        Image(systemName: "gearshape")
                    }
                    .accessibilityLabel("Settings")
                }
            }
        }
    }

    private var isEmptyAgents: Bool {
        !setup.isPaused && (setup.phase == .noAgents || setup.phase == .ready)
    }

    private var title: String {
        isEmptyAgents ? "Agents" : "wristcall"
    }

    @ViewBuilder
    private var content: some View {
        if setup.isPaused || setup.phase == .waitingForPhone {
            waitingForPhone
        } else if isEmptyAgents {
            noAgents
        } else {
            progress
        }
    }

    private var progress: some View {
        let steps = Self.steps(for: setup.phase)
        return VStack(spacing: 8) {
            Image(systemName: "waveform")
                .font(.system(size: 24, weight: .semibold))
                .foregroundStyle(.white)
                .frame(width: 52, height: 52)
                .background(Color.mint.gradient, in: Circle())
                .accessibilityHidden(true)
            Text("Setting up")
                .font(.headline)
            VStack(alignment: .leading, spacing: 6) {
                StepRow(title: steps.signIn == .done ? "Signed in" : "Signing in", state: steps.signIn)
                StepRow(title: "Getting agents", state: steps.agents)
            }
        }
    }

    private var waitingForPhone: some View {
        VStack(spacing: 6) {
            Image(systemName: "hand.raised")
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(.mint)
                .accessibilityHidden(true)
            Text("Open wristcall on your iPhone")
                .font(.headline)
                .multilineTextAlignment(.center)
            Text("Your watch finishes setting up on its own.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if setup.isPaused {
                Button("Set up again") { Task { await setup.setUpAgain() } }
            }
            Button("Use a code instead") { Task { await login.signIn() } }
                .disabled(login.isRunning || setup.isBusy)
        }
    }

    private var noAgents: some View {
        VStack(spacing: 4) {
            Text("No agents yet")
                .font(.headline)
            Text("Add one in wristcall on your iPhone.")
                .font(.footnote)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(.top, 8)
    }
}

/// One line of the progress screen: a spinner while it runs, a mint check once done, dimmed until its turn.
private struct StepRow: View {
    let title: String
    let state: SetupStepState

    var body: some View {
        HStack(spacing: 8) {
            Group {
                switch state {
                case .done:
                    Image(systemName: "checkmark")
                        .font(.footnote.bold())
                        .foregroundStyle(.mint)
                case .active:
                    ProgressView()
                        .controlSize(.small)
                case .pending:
                    Color.clear
                }
            }
            .frame(width: 20, height: 20)
            Text(title)
                .font(.footnote)
                .foregroundStyle(state == .done ? Color.primary : Color.secondary)
        }
        .accessibilityElement(children: .combine)
    }
}
