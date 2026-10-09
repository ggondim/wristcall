import SwiftUI
import WristcallKit

/// Home: the first agent's name, status, the "Call" button (the agent's turn end) and "…" (call
/// options). The agent grid replaces it in task 6.
struct HomeView: View {
    let model: AppModel

    var body: some View {
        NavigationStack {
            VStack(spacing: 8) {
                Text(model.agents.first?.agent.displayName ?? "wristcall")
                    .font(.title3)
                    .lineLimit(1)
                Text(status)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                HStack(spacing: 4) {
                    Button {
                        model.startCall()
                    } label: {
                        Label("Call", systemImage: "phone.fill")
                            .font(.title3)
                            .frame(maxWidth: .infinity, minHeight: 56)
                    }
                    .buttonStyle(.borderedProminent)
                    .tint(.green)
                    NavigationLink {
                        CallOptionsView(model: model)
                    } label: {
                        Image(systemName: "ellipsis")
                            .frame(minHeight: 56)
                    }
                    .buttonStyle(.bordered)
                    .frame(width: 44)
                    .accessibilityLabel("Call options")
                }
                .disabled(!model.canCall)
                if model.phase == .unavailable || model.servers.contains(where: \.isUnavailable) {
                    Button("Retry") {
                        Task { await model.retry() }
                    }
                }
            }
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

    private var status: String {
        if let message = model.message { return message }
        if model.canCall { return "Ready" }
        return model.isLoadingServers ? "Loading…" : AppModel.Message.unreachable
    }
}

/// The app's own call screen (behind the system call UI on the watch): who, what is happening, "End".
/// Mute lives in the system call UI.
struct InCallView: View {
    let model: AppModel
    let target: AgentTarget

    var body: some View {
        VStack(spacing: 8) {
            Text(target.agent.displayName)
                .font(.title3)
            Text(model.callActivity.label)
                .font(.footnote)
                .foregroundStyle(.secondary)
            Button(role: .destructive) {
                model.endCall()
            } label: {
                Label("End", systemImage: "phone.down.fill")
                    .frame(maxWidth: .infinity, minHeight: 56)
            }
            .buttonStyle(.borderedProminent)
            .tint(.red)
        }
    }
}
